"""Background workers that drain the document queue.

A small pool of daemon threads polls `documents` for QUEUED rows, claims one
each with SELECT ... FOR UPDATE SKIP LOCKED, and runs it. Started and stopped
with the FastAPI app lifespan.

CONCURRENCY MODEL
    Workers run in parallel, but only the OCR phase actually overlaps. The
    Tekion phase is wrapped in TEKION_LOCK (see tekion_lock.py) because the
    Tekion client is a shared singleton whose dealership is mutable state.

    So with PIPELINE_WORKERS=3: up to three invoices are being read by Gemini at
    once, while Tekion is talked to strictly one conversation at a time. OCR is
    the slow part, so that is where the parallelism is worth having.

    Set PIPELINE_WORKERS=0 to disable in-process workers entirely — useful if
    you later run them as a separate process.
"""
from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone

from sqlmodel import Session

from api.db import engine
from api.services import job_queue

# Seconds between polls when the queue is empty.
POLL_INTERVAL = float(os.environ.get("PIPELINE_POLL_INTERVAL", "2.0"))
WORKER_COUNT = int(os.environ.get("PIPELINE_WORKERS", "2"))
# How often to sweep for rows abandoned by a dead worker.
SWEEP_INTERVAL_SECONDS = 300

_threads: list[threading.Thread] = []
_stop = threading.Event()
_last_sweep = 0.0
_sweep_lock = threading.Lock()


def _maybe_sweep() -> None:
    """Occasionally reclaim rows left PROCESSING by a worker that died."""
    global _last_sweep
    now = datetime.now(timezone.utc).timestamp()
    with _sweep_lock:
        if now - _last_sweep < SWEEP_INTERVAL_SECONDS:
            return
        _last_sweep = now
    try:
        with Session(engine) as session:
            job_queue.requeue_stale(session)
    except Exception as e:  # noqa: BLE001 — a failed sweep must not kill the worker
        print(f"[WORKER] sweep failed: {e}")


def _loop(worker_id: str) -> None:
    # Imported here so the worker module stays importable without the pipeline.
    from api.services.pipeline_service import run_job

    print(f"[WORKER] {worker_id} started")
    while not _stop.is_set():
        document_id = None
        try:
            _maybe_sweep()
            with Session(engine) as session:
                claimed = job_queue.claim_next(session, worker_id)
                # Read the id inside the session; the row is used no further.
                document_id = claimed.id if claimed is not None else None
        except Exception as e:  # noqa: BLE001
            print(f"[WORKER] {worker_id} claim failed: {e}")
            _stop.wait(POLL_INTERVAL)
            continue

        if document_id is None:
            _stop.wait(POLL_INTERVAL)
            continue

        try:
            run_job(document_id)
        except Exception as e:  # noqa: BLE001 — never let one job kill the worker
            print(f"[WORKER] {worker_id} job {document_id} crashed: {e}")

    print(f"[WORKER] {worker_id} stopped")


def start() -> None:
    """Spawn the worker pool. Idempotent."""
    if _threads:
        return
    if WORKER_COUNT <= 0:
        print("[WORKER] PIPELINE_WORKERS=0 — in-process workers disabled")
        return

    _stop.clear()
    for i in range(WORKER_COUNT):
        worker_id = f"w{i}-{uuid.uuid4().hex[:6]}"
        thread = threading.Thread(target=_loop, args=(worker_id,), daemon=True, name=worker_id)
        thread.start()
        _threads.append(thread)
    print(f"[WORKER] started {WORKER_COUNT} worker(s), polling every {POLL_INTERVAL}s")


def stop(timeout: float = 10.0) -> None:
    """Signal the pool to finish and wait briefly for it."""
    if not _threads:
        return
    print("[WORKER] stopping...")
    _stop.set()
    for thread in _threads:
        thread.join(timeout=timeout)
    _threads.clear()

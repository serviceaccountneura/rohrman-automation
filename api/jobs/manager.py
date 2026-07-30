"""In-memory background job manager for OCR extraction tasks.

Simple dict-based tracking. Upgradeable to Celery + Redis later without
changing the route interfaces.
"""
from __future__ import annotations

import threading
import uuid
from typing import Any

from api.models.schemas import OcrJobStatus


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create_job(self) -> str:
        job_id = str(uuid.uuid4())
        with self._lock:
            self._jobs[job_id] = {
                "status": OcrJobStatus.QUEUED,
                "result": None,
                "error": None,
            }
        return job_id

    def update_job(self, job_id: str, status: OcrJobStatus, result: Any = None, error: str | None = None) -> None:
        with self._lock:
            if job_id not in self._jobs:
                return
            self._jobs[job_id]["status"] = status
            if result is not None:
                self._jobs[job_id]["result"] = result
            if error is not None:
                self._jobs[job_id]["error"] = error

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cleanup_old_jobs(self, max_age_seconds: int = 3600) -> None:
        """Remove jobs older than max_age_seconds. Call periodically if needed."""
        import time

        now = time.time()
        with self._lock:
            to_remove = [
                jid for jid, job in self._jobs.items()
                if job.get("created_at") and now - job["created_at"] > max_age_seconds
            ]
            for jid in to_remove:
                del self._jobs[jid]


# Singleton instance
job_manager = JobManager()

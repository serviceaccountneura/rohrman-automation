"""Serialization for Tekion work.

WHY THIS EXISTS
    `get_client()` in api/routes/tekion.py returns a module-level singleton, and
    the dealership is *mutable state on that object*: `switch_dealer()` sets
    `self.dealer_id`, and `_req()` rebuilds the `dealerid` / `roleid` /
    `tek-siteid` headers from that field on every call.

    So two threads working on different dealerships interleave like this:

        thread A                     thread B
        switch_dealer("1707")
                                     switch_dealer("1711")   <- overwrites A
        create_sublet_po()  ->  posts to 1711, silently wrong

    `_client_lock` in that module only guards *construction* of the client, not
    the switch-then-call sequence, so it does not help here.

THE FIX
    Hold TEKION_LOCK for the whole dealer-scoped operation — from the moment the
    dealer is switched until the last call that depends on it. That serializes
    Tekion work across the API routes and the queue workers alike.

    This is deliberately a lock rather than a refactor of TekionApiClient: making
    `dealer_id` a per-call parameter would touch every method the working PO
    flow depends on. The lock fixes the correctness bug without changing any of
    that code.

    It is an RLock so nesting is safe (a route holding it can call a helper that
    takes it again).

THROUGHPUT
    OCR is the slow part of a job (tens of seconds) and touches no Tekion state,
    so workers run it in parallel. Only the Tekion phase is serialized, which is
    what we want anyway: one conversation with Tekion at a time.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.services.tekion_client import TekionApiClient

# Held for the duration of any dealer-scoped Tekion operation.
TEKION_LOCK = threading.RLock()


@contextmanager
def tekion_scope() -> Iterator[None]:
    """Serialize a block of Tekion work.

    Use around any sequence that switches dealership and then acts on it.
    """
    with TEKION_LOCK:
        yield


@contextmanager
def dealer_scope(client: TekionApiClient, dealership_name: str) -> Iterator[str]:
    """Serialize + switch dealership, yielding the resolved dealer id.

    The switch happens *inside* the lock so no other thread can retarget the
    client between the switch and the calls that follow.

    Raises ValueError when the dealership cannot be matched — better than
    silently acting on whichever dealer the client happened to be pointing at.
    """
    with TEKION_LOCK:
        dealer_id = client.find_dealer_by_name(dealership_name)
        if not dealer_id:
            raise ValueError(f"Could not match dealership '{dealership_name}'")
        client.switch_dealer(dealer_id)
        yield dealer_id

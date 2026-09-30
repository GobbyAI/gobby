"""In-flight reservations that keep concurrent hooks from re-injecting one batch.

Pending messages stay undelivered until a later hook acknowledges the receipt
that staged them, so parallel hooks for one session would each read and inject
the same batch. The first reader reserves the ids; concurrent readers skip them.

A reservation lives for ``HOOK_RECEIPT_REDELIVERY_GRACE``. It starts at read,
before the staging receipt is prepared, so it always lapses before that receipt
becomes eligible for carry-forward. That ordering keeps a carried receipt from
acknowledging ids no hook re-rendered; keep the TTL no longer than the grace.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable

from gobby.storage.hook_receipts import HOOK_RECEIPT_REDELIVERY_GRACE

RESERVATION_TTL_SECONDS = HOOK_RECEIPT_REDELIVERY_GRACE.total_seconds()

_lock = threading.Lock()
_reserved: dict[str, dict[str, float]] = {}


def reserve_pending_messages(
    session_id: str,
    message_ids: Iterable[str],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> set[str]:
    """Reserve the ids no concurrent hook holds for this session and return them."""

    now = clock()
    with _lock:
        held = {
            message_id: deadline
            for message_id, deadline in _reserved.get(session_id, {}).items()
            if deadline > now
        }
        granted = {message_id for message_id in message_ids if message_id not in held}
        held.update(dict.fromkeys(granted, now + RESERVATION_TTL_SECONDS))
        if held:
            _reserved[session_id] = held
        else:
            _reserved.pop(session_id, None)
    return granted


def clear_pending_message_reservations() -> None:
    """Drop every reservation, as a daemon restart does."""

    with _lock:
        _reserved.clear()


def release_pending_messages(session_id: str, message_ids: Iterable[str]) -> None:
    """Lift reservations so the next hook can deliver these ids again."""

    with _lock:
        held = _reserved.get(session_id)
        if held is None:
            return
        for message_id in message_ids:
            held.pop(message_id, None)
        if not held:
            _reserved.pop(session_id, None)

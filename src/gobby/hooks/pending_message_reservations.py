"""In-flight reservations that keep concurrent hooks from re-injecting one batch.

Pending messages stay undelivered until a later hook acknowledges the receipt
that staged them, so parallel hooks for one session would each read and inject
the same batch. The first reader reserves the ids; concurrent readers skip them.

Correctness does not rest on the TTL: a receipt acknowledges only the ids its
own response rendered, and carry-forward never moves pending-message ids onto
another envelope. The TTL only bounds how long a lost delivery waits before a
later hook renders it again. Each grant carries a token, so a late release lifts
only its own grant and never a newer reader's reservation of the same id.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from gobby.storage.hook_receipts import HOOK_RECEIPT_REDELIVERY_GRACE

RESERVATION_TTL_SECONDS = HOOK_RECEIPT_REDELIVERY_GRACE.total_seconds()

_lock = threading.Lock()
_tokens = itertools.count(1)
# session_id -> message_id -> (deadline, token)
_reserved: dict[str, dict[str, tuple[float, int]]] = {}


@dataclass(frozen=True)
class PendingMessageReservation:
    """The ids one reader was granted, bound to that grant's token."""

    token: int
    message_ids: frozenset[str]


def reserve_pending_messages(
    session_id: str,
    message_ids: Iterable[str],
    *,
    clock: Callable[[], float] = time.monotonic,
) -> PendingMessageReservation:
    """Reserve the ids no concurrent hook holds for this session."""

    now = clock()
    with _lock:
        token = next(_tokens)
        held = {
            message_id: grant
            for message_id, grant in _reserved.get(session_id, {}).items()
            if grant[0] > now
        }
        granted = frozenset(message_id for message_id in message_ids if message_id not in held)
        held.update(dict.fromkeys(granted, (now + RESERVATION_TTL_SECONDS, token)))
        if held:
            _reserved[session_id] = held
        else:
            _reserved.pop(session_id, None)
    return PendingMessageReservation(token=token, message_ids=granted)


def clear_pending_message_reservations() -> None:
    """Drop every reservation, as a daemon restart does."""

    with _lock:
        _reserved.clear()


def release_pending_messages(
    session_id: str,
    reservation: PendingMessageReservation,
    message_ids: Iterable[str],
) -> None:
    """Lift this grant's reservation of these ids so the next hook can deliver them."""

    with _lock:
        held = _reserved.get(session_id)
        if held is None:
            return
        for message_id in message_ids:
            grant = held.get(message_id)
            if grant is not None and grant[1] == reservation.token:
                del held[message_id]
        if not held:
            _reserved.pop(session_id, None)

"""Ambient transaction registry for hub database adapters."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol

from gobby.storage.hub.protocol import LockTarget, Transaction

_AMBIENT: ContextVar[Mapping[int, Transaction] | None] = ContextVar(
    "_AMBIENT",
    default=None,
)


class TransactionOpener(Protocol):
    @contextmanager
    def __call__(
        self,
        *,
        immediate: bool,
        lock: LockTarget | None,
    ) -> Iterator[Transaction]: ...


@contextmanager
def enter_transaction(
    adapter: object,
    opener: TransactionOpener,
    *,
    immediate: bool = False,
    lock: LockTarget | None = None,
) -> Iterator[Transaction]:
    """Enter or reuse the ambient transaction for one adapter instance."""
    adapter_id = id(adapter)
    current = dict(_AMBIENT.get() or {})
    existing = current.get(adapter_id)
    if existing is not None:
        if immediate and not existing.is_immediate:
            raise RuntimeError("transaction_immediate() inside a non-immediate transaction()")
        if immediate and lock is not None:
            existing.acquire_additional_lock(lock)
        yield existing
        return

    with opener(immediate=immediate, lock=lock) as txn:
        token = _AMBIENT.set({**current, adapter_id: txn})
        try:
            yield txn
        finally:
            _AMBIENT.reset(token)


def ambient_transaction(adapter: object) -> Transaction | None:
    """Return the active ambient transaction for one adapter instance, if any."""
    current = _AMBIENT.get()
    if current is None:
        return None
    return current.get(id(adapter))

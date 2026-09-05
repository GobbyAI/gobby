"""Context-local bounds for synchronous database work."""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

DEFAULT_DATABASE_OPERATION_TIMEOUT_SECONDS = 5.0


class DatabaseOperationDeadlineExceeded(RuntimeError):
    """Raised before starting database work after its owning deadline."""


@dataclass(frozen=True, slots=True)
class DatabaseOperationDeadline:
    expires_at: float
    operation_timeout_seconds: float

    def remaining_seconds(self, *, maximum_seconds: float | None = None) -> float:
        """Return the bounded time available for the next database operation."""
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise DatabaseOperationDeadlineExceeded("Database operation deadline expired")
        maximum = self.operation_timeout_seconds
        if maximum_seconds is not None:
            maximum = min(maximum, maximum_seconds)
        return min(remaining, maximum)

    def remaining_milliseconds(self) -> int:
        """Return a positive PostgreSQL timeout bounded by the deadline."""
        return max(1, math.ceil(self.remaining_seconds() * 1_000))


_CURRENT_DATABASE_OPERATION_DEADLINE: ContextVar[DatabaseOperationDeadline | None] = ContextVar(
    "database_operation_deadline", default=None
)


def current_database_operation_deadline() -> DatabaseOperationDeadline | None:
    """Return the deadline inherited by the current sync or async context."""
    return _CURRENT_DATABASE_OPERATION_DEADLINE.get()


@contextmanager
def database_operation_deadline(
    *,
    timeout_seconds: float,
    operation_timeout_seconds: float = DEFAULT_DATABASE_OPERATION_TIMEOUT_SECONDS,
) -> Iterator[DatabaseOperationDeadline]:
    """Bound database work owned by a larger operation.

    Nested scopes keep the earliest deadline and shortest per-operation limit.
    Context variables propagate through ``asyncio.to_thread`` and copied executor
    contexts, which keeps synchronous database work tied to its workflow owner.
    """
    if timeout_seconds <= 0 or operation_timeout_seconds <= 0:
        raise ValueError("Database operation deadline bounds must be positive")

    current = current_database_operation_deadline()
    expires_at = time.monotonic() + timeout_seconds
    if current is not None:
        expires_at = min(expires_at, current.expires_at)
        operation_timeout_seconds = min(
            operation_timeout_seconds,
            current.operation_timeout_seconds,
        )
    deadline = DatabaseOperationDeadline(
        expires_at=expires_at,
        operation_timeout_seconds=operation_timeout_seconds,
    )
    token = _CURRENT_DATABASE_OPERATION_DEADLINE.set(deadline)
    try:
        yield deadline
    finally:
        _CURRENT_DATABASE_OPERATION_DEADLINE.reset(token)

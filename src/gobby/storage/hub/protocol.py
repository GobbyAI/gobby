"""Backend-neutral storage protocol for hub database adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, ClassVar, Literal, Protocol

Row = Mapping[str, Any]

__all__ = [
    "Cursor",
    "HubDatabase",
    "LockAcquisitionOrderError",
    "LockTarget",
    "Row",
    "Savepoint",
    "Transaction",
]


class LockAcquisitionOrderError(RuntimeError):
    """Raised when an immediate transaction acquires locks out of priority order.

    Implementations should include both priority values and lock-key strings in
    the error message so callers can diagnose and retry deterministic ordering
    failures instead of waiting on AB/BA deadlocks.
    """


class LockTarget(Protocol):
    """Names the resource protected by a typed immediate transaction lock.

    Concrete dataclass cases are owned by the storage-manager porting work. Each
    concrete subclass pins a class-level ``PRIORITY`` value; nested locks must use
    strictly greater priorities than locks already held by the transaction.
    """

    PRIORITY: ClassVar[int]


class Cursor(Protocol):
    """Backend-neutral cursor surface returned by transaction execution."""

    def fetchone(self) -> Row | None:
        """Return the next row, or ``None`` when the result set is exhausted."""
        ...

    def fetchall(self) -> Sequence[Row]:
        """Return all remaining rows."""
        ...

    @property
    def rowcount(self) -> int:
        """Return rows affected by the preceding statement."""
        ...


class Savepoint(Protocol):
    """Transaction-owned savepoint handle."""

    def release(self) -> None:
        """Release the savepoint."""
        ...

    def rollback(self) -> None:
        """Roll back to the savepoint."""
        ...


class Transaction(Protocol):
    """Backend-neutral transaction executor."""

    is_immediate: bool

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Cursor:
        """Execute a SQL statement and return a backend-neutral cursor."""
        ...

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        """Execute one SQL statement for multiple positional parameter rows."""
        ...

    def savepoint(self, name: str) -> Savepoint:
        """Create a transaction-owned savepoint."""
        ...

    def after_commit(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` after COMMIT returns on the writing session.

        Cross-session reads remain subject to each reader's snapshot. Under
        PostgreSQL MVCC, callbacks that spawn work on another session may observe
        pre-commit state until that session starts a new transaction.
        """
        ...

    def acquire_additional_lock(self, lock: LockTarget) -> None:
        """Acquire another lock within an existing immediate transaction.

        Nested immediate transaction calls delegate here. The new lock priority
        must be strictly greater than the highest ``LockTarget.PRIORITY`` already
        held by this transaction. Implementations raise
        ``LockAcquisitionOrderError`` with both priority values and lock-key
        strings when the check fails. PostgreSQL adapters then acquire the
        matching advisory or row lock inside the existing transaction; SQLite
        adapters still enforce priority tracking for parity even though
        ``BEGIN IMMEDIATE`` already holds the write-intent lock.
        """
        ...


class HubDatabase(Protocol):
    """Backend-neutral database adapter contract for hub storage."""

    dialect: Literal["sqlite", "postgres"]

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        """Open a transaction and yield a backend-neutral executor."""
        ...

    @contextmanager
    def transaction_immediate(self, lock: LockTarget) -> Iterator[Transaction]:
        """Open a write-intent transaction for a typed lock target."""
        ...

    def apply_migrations(self) -> None:
        """Apply pending migrations for the backing database."""
        ...

    def close(self) -> None:
        """Release database resources held by the adapter."""
        ...

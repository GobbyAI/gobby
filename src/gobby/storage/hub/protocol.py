"""Backend-neutral storage protocol for hub database adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Protocol

Row = Mapping[str, Any]

__all__ = [
    "Cursor",
    "ChatAttachmentMutation",
    "DispatchMutexRow",
    "HubDatabase",
    "LockAcquisitionOrderError",
    "LockTarget",
    "Row",
    "Savepoint",
    "SessionRecoveryByProject",
    "SessionRegistration",
    "SystemSessionBootstrap",
    "TaskLifecycleMutation",
    "TaskSeqAllocation",
    "TaskSubtreeCascade",
    "Transaction",
    "WebChatSessionBootstrap",
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


@dataclass(frozen=True)
class TaskSeqAllocation:
    """Serializes per-project task sequence allocation."""

    PRIORITY: ClassVar[int] = 200
    project_id: str


@dataclass(frozen=True)
class DispatchMutexRow:
    """Serializes read-then-upsert dispatch mutex acquisition."""

    PRIORITY: ClassVar[int] = 300
    task_id: str


@dataclass(frozen=True)
class TaskSubtreeCascade:
    """Serializes build-control cascades at project granularity."""

    PRIORITY: ClassVar[int] = 400
    project_id: str


@dataclass(frozen=True)
class TaskLifecycleMutation:
    """Serializes per-task lifecycle updates that need write-intent locking."""

    PRIORITY: ClassVar[int] = 450
    task_id: str


@dataclass(frozen=True)
class WebChatSessionBootstrap:
    """Outer lock for web-chat session bootstrap plus follow-up metadata update."""

    PRIORITY: ClassVar[int] = 500
    external_id: str
    machine_id: str
    source: str
    project_id: str | None
    session_type: str


@dataclass(frozen=True)
class SessionRegistration:
    """Serializes first-time session registration for a natural identity tuple."""

    PRIORITY: ClassVar[int] = 600
    external_id: str
    machine_id: str
    source: str
    project_id: str | None
    session_type: str


@dataclass(frozen=True)
class SessionRecoveryByProject:
    """Serializes project-scoped recovery scans for moved sessions."""

    PRIORITY: ClassVar[int] = 700
    project_id: str


@dataclass(frozen=True)
class SystemSessionBootstrap:
    """Serializes one-time system-session bootstrap."""

    PRIORITY: ClassVar[int] = 800


@dataclass(frozen=True)
class ChatAttachmentMutation:
    """Serializes chat-attachment binding and deletion mutations."""

    PRIORITY: ClassVar[int] = 900


class Cursor(Protocol):
    """Backend-neutral cursor surface returned by transaction execution."""

    def fetchone(self) -> Row | None:
        """Return the next row, or ``None`` when the result set is exhausted."""
        ...

    def fetchall(self) -> list[Row]:
        """Return all remaining rows."""
        ...

    @property
    def rowcount(self) -> int:
        """Return rows affected by the preceding statement."""
        ...

    @property
    def lastrowid(self) -> int | None:
        """Return the SQLite insert row id when the backend exposes one."""
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

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> Cursor:
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

    def transaction(self) -> AbstractContextManager[Transaction]:
        """Open a transaction and yield a backend-neutral executor."""
        ...

    def transaction_immediate(
        self,
        lock: LockTarget | None = None,
    ) -> AbstractContextManager[Transaction]:
        """Open a write-intent transaction for a typed lock target."""
        ...

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Cursor:
        """Execute a SQL statement, joining an ambient transaction when present."""
        ...

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> Cursor:
        """Execute one SQL statement for multiple parameter rows."""
        ...

    def fetchone(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Row | None:
        """Execute a query and fetch one row."""
        ...

    def fetchall(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> list[Row]:
        """Execute a query and fetch all rows."""
        ...

    def safe_update(
        self,
        table: str,
        values: Mapping[str, Any],
        where: str,
        where_params: Sequence[Any] = (),
    ) -> Cursor:
        """Execute a validated dynamic-column update."""
        ...

    def apply_migrations(self) -> None:
        """Apply pending migrations for the backing database."""
        ...

    def close(self) -> None:
        """Release database resources held by the adapter."""
        ...

"""Backend-neutral storage protocol for hub database adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Protocol

Row = Mapping[str, Any]

__all__ = [
    "BuildDryRunMutation",
    "Cursor",
    "ChatAttachmentMutation",
    "CronRunAdmission",
    "DispatchMutexRow",
    "ExpansionApplyMutation",
    "HubDatabase",
    "GitHubIssueTriageMutation",
    "IntegrationWorkspaceMutex",
    "IsolationRegistryReconciliation",
    "LockAcquisitionOrderError",
    "LockTarget",
    "PlanReviewEvidenceMutation",
    "ReviewLearningPatternMutation",
    "Row",
    "Savepoint",
    "SessionLineageMutation",
    "SessionRecoveryByProject",
    "SessionRegistration",
    "SessionSeqMutation",
    "SessionVariableMutation",
    "StageReviewApprovalMutation",
    "StageReviewRejectionMutation",
    "SystemSessionBootstrap",
    "TaskDependencyMutation",
    "TaskLifecycleMutation",
    "TaskSeqAllocation",
    "TaskSubtreeCascade",
    "Transaction",
    "WebChatSessionBootstrap",
    "WorkflowDefinitionMutation",
    "AgentStepInstanceMutation",
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
class BuildDryRunMutation:
    """Outer lock for a dry-run build and its nested task mutations."""

    PRIORITY: ClassVar[int] = 50
    project_id: str


@dataclass(frozen=True)
class AgentCapAdmission:
    """Serializes dispatcher admission against active-agent caps."""

    PRIORITY: ClassVar[int] = 100
    project_id: str | None


@dataclass(frozen=True)
class CronRunAdmission:
    """Serializes cron run admission against global active-run caps."""

    PRIORITY: ClassVar[int] = 100


@dataclass(frozen=True)
class GitHubIssueTriageMutation:
    """Serializes task creation or update for one GitHub issue."""

    PRIORITY: ClassVar[int] = 150
    project_id: str
    repo: str
    issue_number: int


@dataclass(frozen=True)
class ReviewLearningPatternMutation:
    """Serializes review-lesson and guardrail mutations for one pattern."""

    PRIORITY: ClassVar[int] = 175
    project_id: str
    pattern_key: str


@dataclass(frozen=True)
class PlanReviewEvidenceMutation:
    """Serializes evidence lifecycle and plan-file writes for one plan path."""

    PRIORITY: ClassVar[int] = 475
    project_id: str
    plan_path: str


@dataclass(frozen=True)
class TaskSeqAllocation:
    """Serializes per-project task sequence allocation."""

    PRIORITY: ClassVar[int] = 200
    project_id: str


@dataclass(frozen=True)
class ExpansionApplyMutation:
    """Serializes expansion apply for one parent task."""

    PRIORITY: ClassVar[int] = 250
    parent_task_id: str


@dataclass(frozen=True)
class TaskDependencyMutation:
    """Serializes dependency cycle checks and inserts.

    Must stay above ExpansionApplyMutation: apply_run holds that lock while
    add_dependency acquires this one, and nested priorities must increase.
    """

    PRIORITY: ClassVar[int] = 275


@dataclass(frozen=True)
class StageReviewApprovalMutation:
    """Outer lock for atomic stage approval and evidence finalization."""

    PRIORITY: ClassVar[int] = 290
    task_id: str


@dataclass(frozen=True)
class StageReviewRejectionMutation:
    """Outer lock for atomic stage rejection and evidence finalization."""

    PRIORITY: ClassVar[int] = 290
    task_id: str


@dataclass(frozen=True)
class DispatchMutexRow:
    """Serializes read-then-upsert dispatch mutex acquisition."""

    PRIORITY: ClassVar[int] = 300
    task_id: str


@dataclass(frozen=True)
class IntegrationWorkspaceMutex:
    """Serializes lease acquisition for one integration workspace."""

    PRIORITY: ClassVar[int] = 350
    integration_key: str


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
    machine_id: str | None
    source: str
    project_id: str | None
    session_type: str


@dataclass(frozen=True)
class IsolationRegistryReconciliation:
    """Serializes reverse isolation-registry reconciliation for one machine.

    Future nested typed locks acquired during reconciliation must use a priority
    greater than 550.
    """

    PRIORITY: ClassVar[int] = 550
    machine_id: str


@dataclass(frozen=True)
class SessionRegistration:
    """Serializes first-time session registration for a natural identity tuple."""

    PRIORITY: ClassVar[int] = 600
    external_id: str
    source: str
    session_type: str


@dataclass(frozen=True)
class SessionLineageMutation:
    """Serializes session parent cycle checks and mutations."""

    PRIORITY: ClassVar[int] = 650


@dataclass(frozen=True)
class SessionRecoveryByProject:
    """Serializes project-scoped recovery scans for moved sessions."""

    PRIORITY: ClassVar[int] = 700
    project_id: str


@dataclass(frozen=True)
class SessionSeqMutation:
    """Serializes project-scoped session sequence allocation and compaction."""

    PRIORITY: ClassVar[int] = 750
    project_id: str


@dataclass(frozen=True)
class SystemSessionBootstrap:
    """Serializes one-time system-session bootstrap."""

    PRIORITY: ClassVar[int] = 800


@dataclass(frozen=True)
class WorkflowDefinitionMutation:
    """Serializes read-modify-write updates to one workflow definition."""

    PRIORITY: ClassVar[int] = 850
    definition_id: str


@dataclass(frozen=True)
class AgentStepInstanceMutation:
    """Serializes mutations to one agent-step instance."""

    PRIORITY: ClassVar[int] = 875
    session_id: str


@dataclass(frozen=True)
class SessionVariableMutation:
    """Serializes read-modify-write updates to one session variable row."""

    PRIORITY: ClassVar[int] = 950
    session_id: str


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
        """Return the inserted row id when the backend exposes one."""
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
        matching advisory or row lock inside the existing transaction.
        """
        ...


class HubDatabase(Protocol):
    """Backend-neutral database adapter contract for hub storage."""

    dialect: Literal["postgres"]

    @property
    def conninfo(self) -> str:
        """Return the normalized PostgreSQL connection string for async consumers."""
        ...

    def transaction(self) -> AbstractContextManager[Transaction]:
        """Open a transaction and yield a backend-neutral executor."""
        ...

    def bounded_transaction(
        self,
        *,
        statement_timeout_ms: int = 5_000,
        lock_timeout_ms: int = 5_000,
    ) -> AbstractContextManager[Transaction]:
        """Open a transaction with transaction-local statement and lock bounds."""
        ...

    def transaction_immediate(
        self,
        lock: LockTarget,
    ) -> AbstractContextManager[Transaction]:
        """Open a write-intent transaction for a typed lock target."""
        ...

    def advisory_lock(self, lock: LockTarget) -> AbstractAsyncContextManager[None]:
        """Hold a session advisory lock without keeping a transaction open."""
        ...

    def after_commit(self, callback: Callable[[], None]) -> None:
        """Run a callback after the ambient transaction commits, or immediately."""
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

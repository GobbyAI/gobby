"""Runtime facade for task dispatch mutex leases."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.utils.datetime import parse_stored_datetime

DISPATCH_TTL_SECONDS = 600


class RuntimeDispatchMutexError(RuntimeError):
    """Base error for runtime dispatch mutex failures."""


class DispatchMutexUnavailableError(RuntimeDispatchMutexError):
    """Raised when another holder already owns the task dispatch lease."""


@dataclass(slots=True)
class RuntimeDispatchMutex:
    """Context manager for a dispatcher-owned task lease.

    The public configuration is fixed by construction, while internal lease
    fields (_acquired, _released, _run_id) mutate as the lease is used.
    """

    storage: TaskDispatchMutexManager
    task_id: str
    holder: str
    action_kind: str
    ttl_seconds: int
    now: datetime | str | None = None
    borrowed_run_id: str | None = None
    _acquired: bool = field(default=False, init=False)
    _released: bool = field(default=False, init=False)
    _run_id: str | None = field(default=None, init=False)
    _lease_holder: str = field(init=False)

    def __post_init__(self) -> None:
        self._lease_holder = f"{self.holder}:{uuid4()}"

    def __enter__(self) -> RuntimeDispatchMutex:
        if self.borrowed_run_id is not None:
            mutex = self.storage.get_mutex(self.task_id)
            now = parse_stored_datetime(self.now) or datetime.now(UTC)
            lease_until = parse_stored_datetime(mutex.lease_until) if mutex is not None else None
            if (
                mutex is None
                or mutex.run_id != self.borrowed_run_id
                or not isinstance(mutex.lease_holder, str)
                or lease_until is None
                or lease_until < now
                or not self.storage.refresh_mutex_for_run(
                    self.task_id,
                    self.borrowed_run_id,
                    mutex.lease_holder,
                    self.ttl_seconds,
                    now=self.now,
                )
            ):
                msg = (
                    f"dispatch mutex for task {self.task_id!r} is not held by "
                    f"run {self.borrowed_run_id!r}"
                )
                raise DispatchMutexUnavailableError(msg)
            self._lease_holder = mutex.lease_holder
            self._run_id = self.borrowed_run_id
        else:
            acquired = self.storage.acquire_mutex(
                self.task_id,
                holder=self._lease_holder,
                kind=self.action_kind,
                ttl_seconds=self.ttl_seconds,
                run_id=None,
                now=self.now,
            )
            if not acquired:
                msg = f"dispatch mutex for task {self.task_id!r} is held by another dispatcher"
                raise DispatchMutexUnavailableError(msg)

        self._acquired = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.borrowed_run_id is None:
            self.release()

    @property
    def acquired(self) -> bool:
        return self._acquired and not self._released

    @property
    def run_id(self) -> str | None:
        return self._run_id

    def attach(self, run_id: str) -> None:
        """Link a spawned run to the currently held lease."""
        if not self.acquired:
            msg = "cannot attach run id without an active dispatch mutex"
            raise RuntimeDispatchMutexError(msg)
        if not self.storage.attach_run_id(
            self.task_id,
            run_id,
            self._lease_holder,
            now=self.now,
        ):
            msg = f"dispatch mutex for task {self.task_id!r} disappeared before attach"
            raise RuntimeDispatchMutexError(msg)
        self._run_id = run_id

    def mark_attached_run_id(self, run_id: str) -> None:
        """Record a run id already attached by a surrounding storage transaction."""
        if not self.acquired:
            msg = "cannot mark run id without an active dispatch mutex"
            raise RuntimeDispatchMutexError(msg)
        self._run_id = run_id

    def release(self) -> bool:
        """Release this context's lease if it is still held by this holder."""
        if self.borrowed_run_id is not None or self._run_id is not None:
            return False
        if not self._acquired or self._released:
            return False
        released = self.storage.release_mutex(
            self.task_id,
            self._lease_holder,
            now=self.now,
        )
        self._released = True
        return released

    @staticmethod
    def force_release_for_run(storage: TaskDispatchMutexManager, run_id: str) -> int:
        """Release leases attached to a terminal agent or expansion run."""
        return storage.clear_by_run_id(run_id)

    @staticmethod
    def force_release_for_task(storage: TaskDispatchMutexManager, task_id: str) -> bool:
        """Release a task lease when a terminal event has no attached run id."""
        return storage.force_release(task_id)


__all__ = [
    "DISPATCH_TTL_SECONDS",
    "DispatchMutexUnavailableError",
    "RuntimeDispatchMutex",
    "RuntimeDispatchMutexError",
]

"""Runtime facade for task dispatch mutex leases."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from gobby.storage.tasks import TaskDispatchMutexManager

CandidateLoader = Callable[[str], object | None]


class RuntimeDispatchMutexError(RuntimeError):
    """Base error for runtime dispatch mutex failures."""


class DispatchMutexUnavailableError(RuntimeDispatchMutexError):
    """Raised when another holder already owns the task dispatch lease."""


class DispatchCandidateChangedError(RuntimeDispatchMutexError):
    """Raised when a task changes state after the lease is acquired."""


@dataclass
class RuntimeDispatchMutex:
    """Context manager for a dispatcher-owned task lease."""

    storage: TaskDispatchMutexManager
    task_id: str
    holder: str
    action_kind: str
    ttl_seconds: int
    now: datetime | str | None = None
    expected_lifecycle: str | None = None
    expected_status: str | None = None
    candidate_loader: CandidateLoader | None = None
    _acquired: bool = field(default=False, init=False)
    _released: bool = field(default=False, init=False)
    _run_id: str | None = field(default=None, init=False)

    def __enter__(self) -> RuntimeDispatchMutex:
        acquired = self.storage.acquire_mutex(
            self.task_id,
            holder=self.holder,
            kind=self.action_kind,
            ttl_seconds=self.ttl_seconds,
            run_id=None,
            now=self.now,
        )
        if not acquired:
            msg = f"dispatch mutex for task {self.task_id!r} is held by another dispatcher"
            raise DispatchMutexUnavailableError(msg)

        self._acquired = True
        if not self._candidate_tuple_still_matches():
            self.release()
            msg = f"task {self.task_id!r} changed lifecycle/status after dispatch lease acquisition"
            raise DispatchCandidateChangedError(msg)

        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
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
        if not self.storage.attach_run_id(self.task_id, run_id):
            msg = f"dispatch mutex for task {self.task_id!r} disappeared before attach"
            raise RuntimeDispatchMutexError(msg)
        self._run_id = run_id

    def release(self) -> bool:
        """Release this context's lease if it is still held by this holder."""
        if not self._acquired or self._released:
            return False
        released = self.storage.release_mutex(self.task_id, self.holder)
        self._released = True
        return released

    def _candidate_tuple_still_matches(self) -> bool:
        if (
            self.candidate_loader is None
            or self.expected_lifecycle is None
            or self.expected_status is None
        ):
            return True
        candidate = self.candidate_loader(self.task_id)
        return self.candidate_tuple_matches(
            candidate,
            lifecycle=self.expected_lifecycle,
            status=self.expected_status,
        )

    @staticmethod
    def candidate_tuple_matches(
        candidate: object | None,
        *,
        lifecycle: str,
        status: str,
    ) -> bool:
        if candidate is None:
            return False
        candidate_lifecycle, candidate_status = _read_candidate_tuple(candidate)
        return candidate_lifecycle == lifecycle and candidate_status == status

    @staticmethod
    def force_release_for_run(storage: TaskDispatchMutexManager, run_id: str) -> int:
        """Release leases attached to a terminal agent or expansion run."""
        return storage.clear_by_run_id(run_id)

    @staticmethod
    def force_release_for_task(storage: TaskDispatchMutexManager, task_id: str) -> bool:
        """Release a task lease when a terminal event has no attached run id."""
        return storage.force_release(task_id)


def _read_candidate_tuple(candidate: object) -> tuple[str | None, str | None]:
    if isinstance(candidate, Mapping):
        lifecycle = candidate.get("lifecycle")
        status = candidate.get("status")
    else:
        lifecycle = getattr(candidate, "lifecycle", None)
        status = getattr(candidate, "status", None)

    return (
        lifecycle if isinstance(lifecycle, str) else None,
        status if isinstance(status, str) else None,
    )


__all__ = [
    "DispatchCandidateChangedError",
    "DispatchMutexUnavailableError",
    "RuntimeDispatchMutex",
    "RuntimeDispatchMutexError",
]

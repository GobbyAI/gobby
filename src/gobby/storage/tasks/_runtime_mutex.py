"""Runtime facade for task dispatch mutex leases."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, cast

from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

CandidateLoader = Callable[[str], object | None]
RuntimeStageSnapshotState = Literal["ready", "in_progress", "needs_review", "review_approved"]

_ACTIONABLE_STAGE_STATES = frozenset({"ready", "in_progress", "needs_review", "review_approved"})


class RuntimeDispatchMutexError(RuntimeError):
    """Base error for runtime dispatch mutex failures."""


class DispatchMutexUnavailableError(RuntimeDispatchMutexError):
    """Raised when another holder already owns the task dispatch lease."""


class DispatchCandidateChangedError(RuntimeDispatchMutexError):
    """Raised when a task changes state after the lease is acquired."""


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
    expected_stage_name: str | None = None
    expected_stage_state: RuntimeStageSnapshotState | None = None
    expected_stage_updated_at: str | None = None
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
        if not self._candidate_stage_snapshot_still_matches():
            self.release()
            msg = f"task {self.task_id!r} changed current-stage snapshot after dispatch lease"
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

    def mark_attached_run_id(self, run_id: str) -> None:
        """Record a run id already attached by a surrounding storage transaction."""
        if not self.acquired:
            msg = "cannot mark run id without an active dispatch mutex"
            raise RuntimeDispatchMutexError(msg)
        self._run_id = run_id

    def release(self) -> bool:
        """Release this context's lease if it is still held by this holder."""
        if not self._acquired or self._released:
            return False
        released = self.storage.release_mutex(self.task_id, self.holder)
        self._released = True
        return released

    def _candidate_stage_snapshot_still_matches(self) -> bool:
        if self.candidate_loader is None:
            return True
        candidate = self.candidate_loader(self.task_id)
        stage_name, stage_state, stage_updated_at = _read_candidate_stage_snapshot(candidate)
        return self.candidate_stage_snapshot_matches(stage_name, stage_state, stage_updated_at)

    def candidate_stage_snapshot_matches(
        self,
        current_stage_name: str | None = None,
        current_stage_state: str | None = None,
        current_stage_updated_at: str | None = None,
    ) -> bool:
        """True iff current-stage values still match this mutex's snapshot."""
        return _stage_snapshot_matches(
            expected_stage_name=self.expected_stage_name,
            expected_stage_state=self.expected_stage_state,
            expected_stage_updated_at=self.expected_stage_updated_at,
            current_stage_name=current_stage_name,
            current_stage_state=current_stage_state,
            current_stage_updated_at=current_stage_updated_at,
        )

    @staticmethod
    def candidate_snapshot_matches(
        candidate: object | None,
        *,
        stage_name: str | None,
        stage_state: str | None,
        stage_updated_at: str | None,
    ) -> bool:
        """True iff a candidate object still matches a captured stage snapshot."""
        current_stage_name, current_stage_state, current_stage_updated_at = (
            _read_candidate_stage_snapshot(candidate)
        )
        return _stage_snapshot_matches(
            expected_stage_name=stage_name,
            expected_stage_state=_coerce_actionable_stage_state(stage_state),
            expected_stage_updated_at=stage_updated_at,
            current_stage_name=current_stage_name,
            current_stage_state=current_stage_state,
            current_stage_updated_at=current_stage_updated_at,
        )

    @staticmethod
    def force_release_for_run(storage: TaskDispatchMutexManager, run_id: str) -> int:
        """Release leases attached to a terminal agent or expansion run."""
        return storage.clear_by_run_id(run_id)

    @staticmethod
    def force_release_for_task(storage: TaskDispatchMutexManager, task_id: str) -> bool:
        """Release a task lease when a terminal event has no attached run id."""
        return storage.force_release(task_id)


def _stage_snapshot_matches(
    *,
    expected_stage_name: str | None,
    expected_stage_state: str | None,
    expected_stage_updated_at: str | None,
    current_stage_name: str | None,
    current_stage_state: str | None,
    current_stage_updated_at: str | None,
) -> bool:
    if (
        expected_stage_name is None
        or expected_stage_state is None
        or expected_stage_updated_at is None
        or current_stage_name is None
        or current_stage_state is None
        or current_stage_updated_at is None
    ):
        return False
    return (
        current_stage_name == expected_stage_name
        and current_stage_state == expected_stage_state
        and current_stage_updated_at == expected_stage_updated_at
    )


def _read_candidate_stage_snapshot(
    candidate: object | None,
) -> tuple[str | None, str | None, str | None]:
    stage = _read_candidate_current_stage(candidate)
    return _read_stage_name(stage), _read_stage_state(stage), _read_stage_updated_at(stage)


def _read_candidate_current_stage(candidate: object | None) -> object | None:
    if candidate is None:
        return None
    current_stage = _read_field(candidate, "current_stage")
    if current_stage is not None:
        return current_stage
    stages = _read_field(candidate, "stages")
    if isinstance(stages, Sequence) and not isinstance(stages, str | bytes | bytearray):
        pending = [stage for stage in stages if _read_stage_state(stage) != "done"]
        return min(pending, key=_read_stage_position) if pending else None
    if _read_stage_name(candidate) is not None or _read_stage_state(candidate) is not None:
        return candidate
    return None


def _read_stage_name(stage: object | None) -> str | None:
    value = _read_field(stage, "stage_name", _read_field(stage, "name"))
    return value if isinstance(value, str) else None


def _read_stage_state(stage: object | None) -> str | None:
    value = _read_field(stage, "state")
    return value if isinstance(value, str) else None


def _read_stage_updated_at(stage: object | None) -> str | None:
    value = _read_field(stage, "updated_at")
    return value if isinstance(value, str) else None


def _read_stage_position(stage: object) -> int:
    value = _read_field(stage, "position", 0)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _read_field(
    obj: object | None,
    name: str,
    default: object | None = None,
) -> object | None:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return cast(Mapping[str, object | None], obj).get(name, default)
    return getattr(obj, name, default)


def _coerce_actionable_stage_state(
    value: object | None,
) -> RuntimeStageSnapshotState | None:
    if isinstance(value, str) and value in _ACTIONABLE_STAGE_STATES:
        return cast(RuntimeStageSnapshotState, value)
    return None


__all__ = [
    "DispatchCandidateChangedError",
    "DispatchMutexUnavailableError",
    "RuntimeDispatchMutex",
    "RuntimeDispatchMutexError",
    "RuntimeStageSnapshotState",
]

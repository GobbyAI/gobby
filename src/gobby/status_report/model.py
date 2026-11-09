"""Data shapes for the daemon-built operator status report.

The collector fills these from the hub database, git and the agent-run store. The renderer reads
them and nothing else, so report text is a pure function of the collected evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from gobby.storage.worktree_notes import WorktreeNote


@dataclass(frozen=True)
class InFlightTask:
    """An open task that a seat has claimed or that has attestation evidence."""

    seq: int
    title: str
    stage: str | None
    """Latest non-superseded attestation as ``kind/result``; None when none is recorded."""
    evidence_at: datetime | None
    claimed_at: datetime | None


@dataclass(frozen=True)
class ClosedTask:
    seq: int
    title: str
    closed_at: datetime
    claim_to_close: timedelta | None


@dataclass(frozen=True)
class EscalatedTask:
    seq: int
    title: str
    escalated_at: datetime | None
    reason: str | None


@dataclass(frozen=True)
class Landing:
    seq: int
    title: str
    commit_sha: str
    recorded_at: datetime
    verified: bool | None
    """True when the commit is an ancestor of the integration branch; None when unverifiable."""


@dataclass(frozen=True)
class AttentionTask:
    """An open task whose projected state needs an operator-visible mention."""

    seq: int
    title: str
    state: str


@dataclass(frozen=True)
class StalledSeat:
    session_ref: str
    task_seq: int | None
    task_title: str | None


@dataclass(frozen=True)
class WorktreeRow:
    branch: str
    task_seq: int | None
    task_state: str | None
    ahead: int | None
    behind: int | None
    dirty: int | None
    live_seat: bool
    last_commit_at: datetime | None
    git_error: str | None
    # The tree's last commit or dirty change; None when git could not say.
    change_at: datetime | None = None
    note: WorktreeNote | None = None

    @property
    def current_note(self) -> WorktreeNote | None:
        """The note when it is newer than the tree's last change; None renders UNRECONCILED."""
        if self.note is None or self.git_error is not None or self.change_at is None:
            return None
        if self.note.updated_at < self.change_at:
            return None
        return self.note


@dataclass(frozen=True)
class StatusData:
    now: datetime
    window_start: datetime
    in_flight: tuple[InFlightTask, ...]
    closed: tuple[ClosedTask, ...]
    escalated: tuple[EscalatedTask, ...]
    landings: tuple[Landing, ...]
    attention: tuple[AttentionTask, ...]
    decisions: tuple[AttentionTask, ...]
    stalled: tuple[StalledSeat, ...]
    worktrees: tuple[WorktreeRow, ...]
    load_5m: float | None
    load_threshold: float
    gaps: tuple[str, ...]

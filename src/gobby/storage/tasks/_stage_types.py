"""Shared task stage-state dataclasses and transition errors."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from gobby.storage.tasks._stage_registry import ReviewPolicy

StageState5 = Literal["ready", "in_progress", "needs_review", "review_approved", "done"]
ManifestMutation = Literal["add_stage", "remove_stage"]
ManifestMutationReason = Literal[
    "position_at_or_before_current",
    "current_row_not_removable",
    "done_row_not_removable",
    "would_exhaust_terminal_position",
    "stage_already_in_manifest",
    "stage_not_in_manifest",
    "manifest_exhausted",
]


class IllegalStageTransitionError(ValueError):
    """Raised when a stage transition is rejected by policy or source state."""

    def __init__(
        self,
        stage_name: str,
        current_state: StageState5,
        attempted_transition: str,
        review_policy: ReviewPolicy,
    ) -> None:
        self.stage_name = stage_name
        self.current_state = current_state
        self.attempted_transition = attempted_transition
        self.review_policy = review_policy
        super().__init__(
            f"Stage '{stage_name}' in state '{current_state}' cannot "
            f"{attempted_transition} under review_policy={review_policy}"
        )


class NoCurrentStageError(ValueError):
    """Raised when a task manifest has no active stage row."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"Task '{task_id}' has no current stage")


class IllegalManifestMutationError(ValueError):
    """Raised when a structural manifest mutation is rejected."""

    def __init__(
        self,
        task_id: str,
        target_stage_name: str,
        target_position: int | None,
        current_stage_name: str | None,
        current_stage_state: StageState5 | None,
        mutation: ManifestMutation,
        reason: ManifestMutationReason,
    ) -> None:
        self.task_id = task_id
        self.target_stage_name = target_stage_name
        self.target_position = target_position
        self.current_stage_name = current_stage_name
        self.current_stage_state = current_stage_state
        self.mutation = mutation
        self.reason = reason
        super().__init__(
            task_id,
            target_stage_name,
            target_position,
            current_stage_name,
            current_stage_state,
            mutation,
            reason,
        )


class ManifestAlreadyInitializedError(ValueError):
    """Raised when a task already has a different stage manifest."""


@dataclass(frozen=True, slots=True)
class StageState:
    task_id: str
    stage_name: str
    position: int
    state: StageState5
    review_policy: ReviewPolicy
    reviewer_agent: str | None
    entered_at: str | None
    entered_by_session_id: str | None
    completed_at: str | None
    completed_by_session_id: str | None
    completed_commit_sha: str | None
    work_attempt_count: int
    review_round_count: int
    max_work_attempts: int | None
    max_review_rounds: int | None
    artifact_refs: dict[str, str] | None
    notes: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class StageManifestSpec:
    stage_name: str
    position: int
    max_work_attempts: int | None = None
    max_review_rounds: int | None = None

    @classmethod
    def from_position_tuple(cls, value: tuple[str, int]) -> StageManifestSpec:
        return cls(stage_name=value[0], position=value[1])


def _coerce_artifact_refs(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return {str(key): str(item) for key, item in decoded.items()}

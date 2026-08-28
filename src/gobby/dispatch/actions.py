"""Typed action emissions from lifecycle dispatcher rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SpawnAgentAction:
    """Spawn an agent for a task."""

    task_id: str
    task_ref: str
    agent_slug: str
    prompt: str
    initial_variables: dict[str, object] | None = None
    additional_skills: tuple[str, ...] = ()
    model_override: str | None = None
    reasoning_effort: str | None = None
    terminal_backend: Literal["tmux", "native"] = "tmux"

    def __post_init__(self) -> None:
        if self.terminal_backend not in ("tmux", "native"):
            raise ValueError(f"invalid terminal_backend: {self.terminal_backend}")


@dataclass(frozen=True, slots=True)
class StartPipelineAction:
    """Start a configured pipeline for a task stage."""

    task_id: str
    task_ref: str
    stage_name: str
    pipeline_name: str
    dispatch_inputs: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class StartStageAction:
    """Start a ready task manifest stage."""

    task_id: str
    stage_name: str


@dataclass(frozen=True)
class CreateIsolationAction:
    """Create the configured task isolation environment."""

    task_id: str
    task_ref: str
    isolation: str
    base_branch: str | None = None


@dataclass(frozen=True)
class MergeWorkspaceAction:
    """Merge a task workspace into an integration branch or root target branch."""

    task_id: str
    task_ref: str
    backend: Literal["worktree", "clone"]
    target_branch: str
    source_branch: str | None = None
    source_workspace_id: str | None = None
    source_clone_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdvanceStageAction:
    """Advance a task manifest stage through the stage-state manager."""

    task_id: str
    stage_name: str
    method: Literal["complete_stage", "approve_review"]
    by_session_id: str = "dispatcher"
    validation_override_reason: str | None = None


@dataclass(frozen=True)
class AppendAuditMarkerAction:
    """Append a structured audit note to task description."""

    task_id: str
    heading: str
    body: str


@dataclass(frozen=True)
class EscalateAction:
    """Escalate a task for human intervention."""

    task_id: str
    reason: str


type Action = (
    SpawnAgentAction
    | StartPipelineAction
    | StartStageAction
    | CreateIsolationAction
    | MergeWorkspaceAction
    | AdvanceStageAction
    | AppendAuditMarkerAction
    | EscalateAction
)


__all__ = [
    "Action",
    "AdvanceStageAction",
    "AppendAuditMarkerAction",
    "CreateIsolationAction",
    "EscalateAction",
    "MergeWorkspaceAction",
    "SpawnAgentAction",
    "StartPipelineAction",
    "StartStageAction",
]

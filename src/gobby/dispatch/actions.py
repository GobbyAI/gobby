"""Typed action emissions from lifecycle dispatcher rules."""

from __future__ import annotations

from dataclasses import dataclass


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


@dataclass(frozen=True)
class StartExpansionAction:
    """Start deterministic expansion for an epic or planning anchor."""

    task_id: str
    task_ref: str


@dataclass(frozen=True)
class CreateIsolationAction:
    """Create the configured task isolation environment."""

    task_id: str
    task_ref: str
    isolation: str
    base_branch: str | None = None


@dataclass(frozen=True)
class AdvanceLifecycleAction:
    """Advance a task from one lifecycle/status tuple to another."""

    task_id: str
    from_lifecycle: str
    from_status: str
    to_lifecycle: str
    to_status: str
    reason: str
    by_actor: str = "dispatcher"


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
    | StartExpansionAction
    | CreateIsolationAction
    | AdvanceLifecycleAction
    | AppendAuditMarkerAction
    | EscalateAction
)


__all__ = [
    "Action",
    "AdvanceLifecycleAction",
    "AppendAuditMarkerAction",
    "CreateIsolationAction",
    "EscalateAction",
    "SpawnAgentAction",
    "StartExpansionAction",
]

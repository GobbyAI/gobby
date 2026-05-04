"""Agent and task lifecycle hook models: subagent, task, teammate-idle."""

from typing import Any

from pydantic import Field

from .base import HookInput, HookOutput


class SubagentStartInput(HookInput):
    """
    Input model for subagent-start hook.

    Triggered when a subagent (spawned via Task tool) starts.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    subagent_id: str | None = Field(default=None, description="Legacy subagent identifier")
    agent_id: str | None = Field(default=None, description="Agent ID of the subagent")
    agent_type: str | None = Field(default=None, description="Agent type of the subagent")
    agent_transcript_path: str | None = Field(
        default=None,
        description="Path to the subagent's transcript file",
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class SubagentStartOutput(HookOutput):
    """Output model for subagent-start hook."""

    additional_context: str | None = Field(
        default=None,
        alias="additionalContext",
        description="Context injected into the spawned subagent",
    )


class SubagentStopInput(HookInput):
    """
    Input model for subagent-stop hook.

    Triggered when a subagent (spawned via Task tool) stops.
    """

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    subagent_id: str | None = Field(default=None, description="Legacy subagent identifier")
    agent_id: str | None = Field(default=None, description="Agent ID of the subagent")
    agent_type: str | None = Field(default=None, description="Agent type of the subagent")
    agent_transcript_path: str | None = Field(
        default=None,
        description="Path to the subagent's transcript file",
    )
    reason: str | None = Field(default=None, description="Reason for stopping subagent")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class SubagentStopOutput(HookOutput):
    """Output model for subagent-stop hook."""

    pass  # Uses base HookOutput fields only


class TaskCreatedInput(HookInput):
    """Input model for task-created hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    task_id: str = Field(..., min_length=1, description="Identifier of the created task")
    task_subject: str = Field(..., min_length=1, description="Task subject/title")
    task_description: str | None = Field(default=None, description="Detailed task description")
    teammate_name: str | None = Field(default=None, description="Teammate creating the task")
    team_name: str | None = Field(default=None, description="Team name, if any")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class TaskCreatedOutput(HookOutput):
    """Output model for task-created hook."""

    pass


class TaskCompletedInput(HookInput):
    """Input model for task-completed hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    task_id: str = Field(..., min_length=1, description="Identifier of the completed task")
    task_subject: str = Field(..., min_length=1, description="Task subject/title")
    task_description: str | None = Field(default=None, description="Detailed task description")
    teammate_name: str | None = Field(default=None, description="Teammate completing the task")
    team_name: str | None = Field(default=None, description="Team name, if any")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class TaskCompletedOutput(HookOutput):
    """Output model for task-completed hook."""

    pass


class TeammateIdleInput(HookInput):
    """Input model for teammate-idle hook."""

    external_id: str = Field(..., min_length=1, description="Unique session identifier")
    teammate_name: str = Field(..., min_length=1, description="Name of the teammate")
    team_name: str = Field(..., min_length=1, description="Team name")
    machine_id: str | None = Field(default=None, description="Unique machine identifier")


class TeammateIdleOutput(HookOutput):
    """Output model for teammate-idle hook."""

    pass

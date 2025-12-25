from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# --- Workflow Definition Models (YAML) ---


class WorkflowRule(BaseModel):
    when: str
    action: Literal["block", "allow", "require_approval", "warn"]
    message: str | None = None


class WorkflowTransition(BaseModel):
    to: str
    when: str
    on_transition: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowExitCondition(BaseModel):
    type: str

    # Other fields depend on type (e.g. pattern, prompt, variable)
    class Config:
        extra = "allow"


class WorkflowPhase(BaseModel):
    name: str
    description: str | None = None

    on_enter: list[dict[str, Any]] = Field(default_factory=list)

    # "all" or list of tool names
    allowed_tools: list[str] | Literal["all"] = Field(default="all")
    blocked_tools: list[str] = Field(default_factory=list)

    rules: list[WorkflowRule] = Field(default_factory=list)
    transitions: list[WorkflowTransition] = Field(default_factory=list)
    exit_conditions: list[dict[str, Any]] = Field(default_factory=list)  # flexible for now

    on_exit: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowDefinition(BaseModel):
    name: str
    description: str | None = None
    version: str = "1.0"
    type: Literal["lifecycle", "phase"] = "phase"
    extends: str | None = None

    settings: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)

    phases: list[WorkflowPhase] = Field(default_factory=list)

    # Global triggers (on_session_start, etc.)
    triggers: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)

    on_error: list[dict[str, Any]] = Field(default_factory=list)

    def get_phase(self, phase_name: str) -> WorkflowPhase | None:
        for p in self.phases:
            if p.name == phase_name:
                return p
        return None


# --- Workflow State Models (Runtime) ---


class WorkflowState(BaseModel):
    session_id: str
    workflow_name: str
    phase: str
    phase_entered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    phase_action_count: int = 0
    total_action_count: int = 0

    artifacts: dict[str, str] = Field(default_factory=dict)
    observations: list[dict[str, Any]] = Field(default_factory=list)

    reflection_pending: bool = False
    context_injected: bool = False

    variables: dict[str, Any] = Field(default_factory=dict)

    # Task decomposition state
    task_list: list[dict[str, Any]] | None = None
    current_task_index: int = 0
    files_modified_this_task: int = 0

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

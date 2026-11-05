"""Request bodies for the agent definition create and update routes.

Split from agents.py to keep the route module under the line ceiling. Unknown keys
are rejected; lifetime and authority keys are immutable and never appear here.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gobby.agents.reasoning import normalize_reasoning_effort
from gobby.workflows.agent_models import AgentModelFallback, AgentPromptBlocks

_LEGACY_STEP_KEYS = {
    "steps": "step_workflow.steps",
    "step_variables": "step_workflow.variables",
    "exit_condition": "step_workflow.exit_condition",
}


def _reject_legacy_step_keys(data: Any) -> Any:
    if isinstance(data, dict):
        for key, hint in _LEGACY_STEP_KEYS.items():
            if key in data:
                raise ValueError(f"Use {hint} instead of top-level {key}")
        legacy_prompts = [
            key for key in ("role", "goal", "personality", "instructions") if key in data
        ]
        if legacy_prompts:
            named = ", ".join(legacy_prompts)
            raise ValueError(
                f"Legacy prompt fields are no longer accepted: {named}. "
                "Use prompts.persona for interactive guidance and prompts.agent "
                "for spawned-run instructions."
            )
    return data


class CreateAgentDefinitionRequest(BaseModel):
    """Request body for creating an agent definition in the DB.

    Unknown keys are rejected: a field the agent body does not carry would
    otherwise be dropped silently.
    Lifetime and authority fields (including persistent_workspace,
    read_all_agent_workspaces and task_attestation_writes) are operator-configured.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    project_id: str | None = None
    description: str | None = None
    version: str | None = None
    sources: list[str] | None = None
    surfaces: list[str] | None = None
    prompts: AgentPromptBlocks = Field(default_factory=AgentPromptBlocks)
    provider: str = "inherit"
    model: str | None = None
    reasoning_effort: str | None = None
    reasoning_required: bool | None = None
    fallback_agent: str | None = None
    fallbacks: list[AgentModelFallback] | None = None
    checkout_mode: str | None = "inherit"
    base_branch: str = "inherit"
    timeout: float = 0
    prewarm_pre_commit_store: bool = True
    workflows: dict[str, Any] | None = None
    blocked_tools: list[str] | None = None
    blocked_mcp_tools: list[str] | None = None
    implementation_domains: list[str] | None = None
    task_categories: list[str] | None = None
    enabled: bool = True
    tags: list[str] | None = None
    step_workflow: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_steps(cls, data: Any) -> Any:
        return _reject_legacy_step_keys(data)

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_reasoning_effort(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_reasoning_effort(str(value))


class UpdateAgentDefinitionRequest(BaseModel):
    """Editable definition fields; operator lifetime/authority keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    version: str | None = None
    sources: list[str] | None = None
    surfaces: list[str] | None = None
    prompts: AgentPromptBlocks | None = None
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    reasoning_required: bool | None = None
    fallback_agent: str | None = None
    fallbacks: list[AgentModelFallback] | None = None
    checkout_mode: str | None = None
    base_branch: str | None = None
    timeout: float | None = None
    prewarm_pre_commit_store: bool | None = None
    workflows: dict[str, Any] | None = None
    step_workflow: dict[str, Any] | None = None
    enabled: bool | None = None
    blocked_tools: list[str] | None = None
    blocked_mcp_tools: list[str] | None = None
    implementation_domains: list[str] | None = None
    task_categories: list[str] | None = None
    tags: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_steps(cls, data: Any) -> Any:
        return _reject_legacy_step_keys(data)

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_reasoning_effort(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_reasoning_effort(str(value))

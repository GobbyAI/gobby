"""Agent definition models, including the nested step-workflow body."""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

from gobby.agents.reasoning import normalize_reasoning_effort
from gobby.workflows.definitions import WorkflowStep


class AgentSelector(BaseModel):
    """Selector for dynamically filtering rules, variables, and skills."""

    include: list[str] = Field(default_factory=lambda: ["*"])
    exclude: list[str] = Field(default_factory=list)


class AgentWorkflows(BaseModel):
    """Structured orchestration container for an agent definition.

    Replaces the old dict[str, WorkflowSpec] map with explicit typed fields:
    - pipeline: optional named pipeline (DB-backed) to auto-start
    - rules: rule names to activate for this agent type
    - variables: pre-seed session variables (override rule defaults)
    """

    pipeline: str | None = None
    rules: list[str] = Field(default_factory=list)
    rule_selectors: AgentSelector | None = None
    variable_selectors: AgentSelector | None = None
    skill_selectors: AgentSelector | None = None
    skill_format: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)


class AgentStepWorkflowBody(BaseModel):
    """Optional nested step program owned by an agent definition."""

    variables: dict[str, Any] = Field(default_factory=dict)
    exit_condition: str | None = None
    steps: list[WorkflowStep] = Field(min_length=1)

    def get_step(self, step_name: str) -> WorkflowStep | None:
        """Return the named step, or None if it is absent."""
        for step in self.steps:
            if step.name == step_name:
                return step
        return None


class AgentDefinitionBody(BaseModel):
    """Stored as definition_json in workflow_definitions for workflow_type='agent'.

    Agent identity with structured prompt fields, provider config,
    spawn parameters, and orchestration. Behavior is defined by rules
    and optional pipeline, not embedded workflows.
    """

    @model_validator(mode="before")
    @classmethod
    def normalize_empty_strings(cls, data: Any) -> Any:
        """Replace empty strings with 'inherit' for Literal fields that don't accept ''."""
        if isinstance(data, dict):
            defaults = {"isolation": "inherit", "provider": "inherit"}
            for field, default in defaults.items():
                if field in data and data[field] == "":
                    data[field] = default
            if "surfaces" in data and data["surfaces"] == "":
                data["surfaces"] = ["spawn"]
        return data

    name: str
    description: str | None = None
    sources: list[str] | None = None  # Session sources this agent applies to (None = all)
    surfaces: list[Literal["spawn", "persona"]] = Field(
        default_factory=lambda: cast(list[Literal["spawn", "persona"]], ["spawn"]),
        description="Where this definition can be used: spawned execution, session personas, or both.",
    )
    # Structured prompt fields (composed into preamble at spawn time)
    role: str | None = None
    goal: str | None = None
    personality: str | None = None
    instructions: str | None = None
    # Execution
    provider: str = "inherit"
    model: StrictStr | None = None
    reasoning_effort: StrictStr | None = None
    reasoning_required: StrictBool | None = None
    fallback_agent: StrictStr | None = None
    api_base: StrictStr | None = Field(
        default=None,
        description="API base URL for the model endpoint (e.g., http://localhost:1234/v1 for LM Studio)",
    )
    api_token: StrictStr | None = Field(
        default=None,
        description="Auth token for the endpoint. Supports ${ENV_VAR} pattern for env var expansion.",
    )
    model_config = ConfigDict(extra="ignore")  # Tolerate stale YAML with removed fields

    isolation: Literal["none", "worktree", "clone", "inherit"] | None = "inherit"
    base_branch: str = "inherit"
    timeout: float = 0
    # Orchestration
    workflows: AgentWorkflows = Field(default_factory=AgentWorkflows)
    enabled: bool = True
    skills: dict[str, list[str]] = Field(default_factory=dict)
    # Agent-level tool restrictions (applied regardless of step workflow)
    blocked_tools: list[str] = Field(default_factory=list)
    blocked_mcp_tools: list[str] = Field(default_factory=list)
    step_workflow: AgentStepWorkflowBody | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_step_keys(cls, data: Any) -> Any:
        """Reject removed top-level step keys so extra=ignore cannot drop them."""
        if not isinstance(data, dict):
            return data
        replacements = {
            "steps": "step_workflow.steps",
            "step_variables": "step_workflow.variables",
            "exit_condition": "step_workflow.exit_condition",
        }
        present = [key for key in replacements if key in data]
        if present:
            named = ", ".join(f"{key} (use {replacements[key]})" for key in present)
            raise ValueError(f"top-level step fields are no longer accepted: {named}")
        return data

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_reasoning_effort(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("reasoning_effort must be a string")
        return normalize_reasoning_effort(value)

    @field_validator("surfaces", mode="before")
    @classmethod
    def _normalize_surfaces(cls, value: Any) -> list[str]:
        if value is None or value == "":
            return ["spawn"]
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            normalized: list[str] = []
            for item in value:
                if not isinstance(item, str):
                    raise ValueError("surfaces entries must be strings")
                if item not in normalized:
                    normalized.append(item)
            return normalized
        raise ValueError("surfaces must be a string or list of strings")

    def supports_surface(self, surface: Literal["spawn", "persona"]) -> bool:
        """Return True when the definition explicitly supports the requested usage surface."""
        return surface in self.surfaces

    def build_prompt_preamble(self) -> str | None:
        """Build structured prompt preamble from role/goal/personality/instructions."""
        parts = []
        if self.role:
            parts.append(f"## Role\n{self.role}")
        if self.goal:
            parts.append(f"## Goal\n{self.goal}")
        if self.personality:
            parts.append(f"## Personality\n{self.personality}")
        if self.instructions:
            parts.append(f"## Instructions\n{self.instructions}")
        return "\n\n".join(parts) if parts else None

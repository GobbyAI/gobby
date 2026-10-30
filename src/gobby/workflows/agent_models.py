"""Agent definition models, including the nested step-workflow body."""

from __future__ import annotations

from typing import Any, Literal, cast

from jinja2 import TemplateError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StrictBool,
    StrictStr,
    ValidationInfo,
    field_validator,
    model_validator,
)

from gobby.agents.reasoning import normalize_reasoning_effort
from gobby.tasks.categories import DEVELOPMENT_FORWARD_LEAF_CATEGORIES, IMPLEMENTATION_DOMAINS
from gobby.workflows.definitions import WorkflowStep

# The spawnable_agents value that lets a spawned agent spawn any agent.
SPAWN_ANY_AGENT = "*"

SendMessageTarget = Literal["global", "project", "parent", "session", "agent"]


def _parent_only() -> list[SendMessageTarget]:
    return ["parent"]


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
    rule_selectors: AgentSelector
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


class AgentPromptBlocks(BaseModel):
    """Surface-specific, complete prompt preambles for an agent definition."""

    persona: StrictStr | None = None
    agent: StrictStr | None = None
    model_config = ConfigDict(extra="forbid")

    @field_validator("persona", "agent")
    @classmethod
    def _require_non_empty_block(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("prompt blocks must be non-empty strings")
        return value


class AgentTaskScope(BaseModel):
    """Optional task constraints for a definition's step writes or closes."""

    model_config = ConfigDict(extra="forbid")
    task_types: list[StrictStr] | None = None
    categories: list[StrictStr] | None = None
    own_claim_only: StrictBool = False


class AgentDefinitionBody(BaseModel):
    """Stored as definition_json on agent_definitions.

    Agent identity with surface-specific prompt blocks, provider config,
    spawn parameters, and orchestration. Behavior is defined by rules
    and optional pipeline, not embedded workflows. Skill discovery lives in
    workflows.skill_selectors and required loads in
    step_workflow.variables.required_skills.
    """

    @model_validator(mode="before")
    @classmethod
    def normalize_empty_strings(cls, data: Any) -> Any:
        """Replace empty strings with 'inherit' for Literal fields that don't accept ''."""
        if isinstance(data, dict):
            defaults = {"checkout_mode": "inherit", "provider": "inherit"}
            for field, default in defaults.items():
                if field in data and data[field] == "":
                    data[field] = default
            if "surfaces" in data and data["surfaces"] == "":
                data["surfaces"] = ["spawn"]
        return data

    name: str
    description: str | None = None
    version: StrictStr | None = None
    sources: list[str] | None = None  # Session sources this agent applies to (None = all)
    surfaces: list[Literal["spawn", "persona"]] = Field(
        default_factory=lambda: cast(list[Literal["spawn", "persona"]], ["spawn"]),
        description="Where this definition can be used: spawned execution, session personas, or both.",
    )
    prompts: AgentPromptBlocks = Field(default_factory=AgentPromptBlocks)
    # Execution
    execution_mode: Literal["one_shot", "interactive"] = Field(
        default="one_shot",
        description="Run lifetime: one task, or a standing seat that remains available between turns.",
    )
    idle_ttl_seconds: PositiveInt | None = Field(
        default=None,
        description="Idle seconds after which an interactive run ends.",
    )
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

    network: Literal["none", "trusted"] = Field(
        default="none",
        description=(
            "Default SRT egress profile; bundled templates may store `trusted`. "
            "Authorized callers may override the profile for one spawn."
        ),
    )
    # False skips the sandbox's pre-commit store prewarm for definitions that never commit.
    prewarm_pre_commit_store: StrictBool = True
    checkout_mode: Literal["none", "worktree", "clone", "inherit"] | None = "inherit"
    base_branch: str = "inherit"
    timeout: float = 0
    # One workspace folder survives the /clear successors of this definition (addendum 9.3).
    persistent_workspace: StrictBool = False
    # Authority to read every seat's folder, including through managed SRT personas.
    read_all_agent_workspaces: StrictBool = False
    # Orchestration
    workflows: AgentWorkflows
    enabled: bool = True
    # Agents a spawned run of this definition may spawn, enforced by the bundled
    # limit-spawnable-agents rule: [SPAWN_ANY_AGENT] for any, named agents, or
    # empty (the default) for none.
    spawnable_agents: list[str] = Field(default_factory=list)
    # Existing seats this definition may resume within its admitted team. Fresh
    # spawns still require spawnable_agents; team membership and stored type are checked.
    resumable_team_agents: list[str] = Field(default_factory=list)
    # Agent-level tool restrictions (applied regardless of step workflow)
    blocked_tools: list[str] = Field(default_factory=list)
    blocked_mcp_tools: list[str] = Field(default_factory=list)
    # send_message target modes a spawned agent of this type may use, enforced by
    # the bundled rule scope-spawned-agent-send-message
    send_message_targets: list[SendMessageTarget] = Field(default_factory=_parent_only)
    # Grants only the configured, enrolled operator destination, never arbitrary recipients.
    operator_messaging: StrictBool = False
    communications_admin: StrictBool = False
    cold_start_commands: list[StrictStr] = Field(
        default_factory=list,
        description="Bare Gobby command aliases that start this interactive definition.",
    )
    cold_start_counterpart: StrictStr | None = Field(
        default=None,
        description="Optional enabled definition to reuse or open beside this cold-started pane.",
    )
    capabilities: list[StrictStr] = Field(
        default_factory=list,
        description="Permissions granted to this definition, independent of its name.",
    )
    task_categories: list[StrictStr] = Field(
        default_factory=list,
        description="Expansion categories this definition accepts for automatic assignment.",
    )
    implementation_domains: list[StrictStr] = Field(
        default_factory=list,
        description="Optional code-domain restriction for automatic assignment; empty accepts all.",
    )
    message_sender_capabilities: list[StrictStr] = Field(
        default_factory=list,
        description=(
            "Incoming seat messages require any listed sender capability; empty allows all. "
            "The daemon and the operator's own terminal are exempt."
        ),
    )
    monitor_report_recipients: list[StrictStr] = Field(
        default_factory=list,
        description="Enabled definitions that must receive the same report to finish a monitor tick.",
    )
    task_step_writes: list[Literal["test", "implementation", "review", "closing"]] = Field(
        default_factory=list,
        description="Task step records this definition may write through dedicated tools.",
    )
    task_step_scope: AgentTaskScope = Field(default_factory=AgentTaskScope)
    task_close_scope: AgentTaskScope = Field(default_factory=AgentTaskScope)
    respect_task_protections: StrictBool = Field(
        default=False,
        description="Apply the project's protected-task policy to mutations and their ancestors.",
    )
    task_close_reasons: list[StrictStr] | None = Field(
        default=None, description="Allowed close reasons; None leaves reasons unrestricted."
    )
    required_review_capabilities: list[StrictStr] = Field(
        default_factory=list,
        description="All permissions the approved review must record before this definition closes.",
    )
    supplemental_review: StrictBool = Field(
        default=False,
        description=(
            "Review approvals supplement the primary review without changing its verdict. "
            "They require a running agent seat and cannot reject a candidate."
        ),
    )
    step_workflow: AgentStepWorkflowBody | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_step_keys(cls, data: Any) -> Any:
        """Reject removed keys so extra=ignore cannot silently drop them."""
        if not isinstance(data, dict):
            return data
        if "isolation" in data:
            raise ValueError("top-level isolation is no longer accepted: use checkout_mode")
        replacements = {
            "steps": "step_workflow.steps",
            "step_variables": "step_workflow.variables",
            "exit_condition": "step_workflow.exit_condition",
        }
        present = [key for key in replacements if key in data]
        if present:
            named = ", ".join(f"{key} (use {replacements[key]})" for key in present)
            raise ValueError(f"top-level step fields are no longer accepted: {named}")

        if "skills" in data:
            raise ValueError(
                "top-level skills map is no longer accepted: use "
                "workflows.skill_selectors for discovery and "
                "step_workflow.variables.required_skills for required loads"
            )

        legacy_prompt_fields = [
            key for key in ("role", "goal", "personality", "instructions") if key in data
        ]
        if legacy_prompt_fields:
            named = ", ".join(legacy_prompt_fields)
            raise ValueError(
                f"legacy prompt fields are no longer accepted: {named}; "
                "move interactive guidance to prompts.persona and spawned-run "
                "instructions to prompts.agent"
            )
        return data

    @model_validator(mode="after")
    def require_interactive_idle_ttl(self) -> AgentDefinitionBody:
        if self.idle_ttl_seconds is not None and self.execution_mode != "interactive":
            raise ValueError("idle_ttl_seconds requires interactive execution_mode")
        return self

    @model_validator(mode="after")
    def require_surface_prompt_blocks(self) -> AgentDefinitionBody:
        """Require one complete prompt block for every declared usage surface."""
        if "persona" in self.surfaces and self.prompts.persona is None:
            raise ValueError(
                "surfaces includes 'persona', so prompts.persona must be a non-empty string"
            )
        if "spawn" in self.surfaces and self.prompts.agent is None:
            raise ValueError(
                "surfaces includes 'spawn', so prompts.agent must be a non-empty string"
            )
        return self

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

    @field_validator("spawnable_agents")
    @classmethod
    def _spawn_any_stands_alone(cls, value: list[str]) -> list[str]:
        if SPAWN_ANY_AGENT in value and value != [SPAWN_ANY_AGENT]:
            raise ValueError(
                f"spawnable_agents is [{SPAWN_ANY_AGENT!r}] for any agent, "
                "or a list of agent names, never both"
            )
        return value

    def may_spawn(self, agent: str) -> bool:
        """Whether a spawned run of this definition may spawn ``agent``."""
        return self.spawnable_agents == [SPAWN_ANY_AGENT] or agent in self.spawnable_agents

    @field_validator("task_categories", "implementation_domains")
    @classmethod
    def _validate_task_routing(cls, value: list[str], info: ValidationInfo) -> list[str]:
        allowed = (
            DEVELOPMENT_FORWARD_LEAF_CATEGORIES
            if info.field_name == "task_categories"
            else IMPLEMENTATION_DOMAINS
        )
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"Unsupported {info.field_name}: {sorted(unknown)}")
        return list(dict.fromkeys(value))

    @field_validator("cold_start_commands")
    @classmethod
    def _normalize_cold_start_commands(cls, value: list[str]) -> list[str]:
        if any(not name or any(char.isspace() for char in name) for name in value):
            raise ValueError("cold_start_commands entries must be nonempty single words")
        normalized = list(dict.fromkeys(name.casefold() for name in value))
        if any(name in {"help", "skill"} for name in normalized):
            raise ValueError("cold_start_commands cannot replace help or skill")
        return normalized

    def supports_surface(self, surface: Literal["spawn", "persona"]) -> bool:
        """Return True when the definition explicitly supports the requested usage surface."""
        return surface in self.surfaces

    def supports_cold_start(self) -> bool:
        """Whether aliases may activate this definition in an interactive pane."""
        return (
            self.enabled
            and self.supports_surface("persona")
            and not self.workflows.pipeline
            and bool(self.cold_start_commands)
        )

    def prompt_for(
        self,
        surface: Literal["persona", "agent"],
        *,
        variables: dict[str, Any] | None = None,
    ) -> str:
        """Return the complete preamble owned by the requested prompt surface."""
        definition_surface: Literal["persona", "spawn"] = (
            "persona" if surface == "persona" else "spawn"
        )
        if not self.supports_surface(definition_surface):
            raise ValueError(
                f"Agent definition '{self.name}' does not support the "
                f"'{definition_surface}' surface"
            )
        prompt = self.prompts.persona if surface == "persona" else self.prompts.agent
        if prompt is None:  # Defensive guard for model_construct() and mock boundaries.
            raise ValueError(f"Agent definition '{self.name}' has no prompts.{surface} block")
        if "{{" not in prompt and "{%" not in prompt:
            return prompt
        from gobby.workflows.templates import TemplateEngine

        context = {"seat_context": {}, "project_policy": {}, **(variables or {})}
        for name in ("seat_context", "project_policy"):
            if context[name] is None:
                context[name] = {}
        try:
            return TemplateEngine().render(prompt, context)
        except TemplateError as exc:
            raise ValueError(
                f"Agent definition '{self.name}' has invalid prompts.{surface} template: {exc}"
            ) from exc

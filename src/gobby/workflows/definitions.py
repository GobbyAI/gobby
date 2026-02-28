from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Workflow Definition Models (YAML) ---


class RuleDefinition(BaseModel):
    """Named rule definition for block_tools format.

    Can be defined at workflow level (rule_definitions) or in shared rule files.
    """

    tools: list[str] = Field(default_factory=list)
    mcp_tools: list[str] = Field(default_factory=list)
    when: str | None = None
    reason: str
    action: Literal["block", "allow", "warn"] = "block"
    command_pattern: str | None = None
    command_not_pattern: str | None = None

    def to_block_rule(self) -> dict[str, Any]:
        """Convert to block_tools rule dict format."""
        rule: dict[str, Any] = {"reason": self.reason}
        if self.tools:
            rule["tools"] = self.tools
        if self.mcp_tools:
            rule["mcp_tools"] = self.mcp_tools
        if self.when:
            rule["when"] = self.when
        if self.command_pattern:
            rule["command_pattern"] = self.command_pattern
        if self.command_not_pattern:
            rule["command_not_pattern"] = self.command_not_pattern
        return rule


class RuleEvent(str, Enum):
    """Events that rules can respond to."""

    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    BEFORE_AGENT = "before_agent"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    STOP = "stop"
    PRE_COMPACT = "pre_compact"


class RuleEffect(BaseModel):
    """What happens when a rule fires. Five primitive effect types."""

    type: Literal["block", "set_variable", "inject_context", "mcp_call", "observe"]

    # Per-effect condition (gates this individual effect within a multi-effect rule)
    when: str | None = None

    # block — prevent the action
    reason: str | None = None
    tools: list[str] | None = None
    mcp_tools: list[str] | None = None
    command_pattern: str | None = None
    command_not_pattern: str | None = None

    # set_variable — update session/workflow state
    variable: str | None = None
    value: Any = None

    # inject_context — add text to system message
    template: str | None = None

    # mcp_call — call an MCP tool
    server: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    background: bool = False

    # observe — append structured entry to _observations session variable
    category: str | None = None
    message: str | None = None

    def model_post_init(self, __context: Any) -> None:
        """Warn when fields irrelevant to the effect type are set."""
        import warnings

        _fields_by_type: dict[str, set[str]] = {
            "block": {"reason", "tools", "mcp_tools", "command_pattern", "command_not_pattern"},
            "set_variable": {"variable", "value"},
            "inject_context": {"template"},
            "mcp_call": {"server", "tool", "arguments", "background"},
            "observe": {"category", "message"},
        }
        # Fields with non-None defaults that shouldn't trigger warnings
        _default_skip = {"background", "when"}
        relevant = _fields_by_type.get(self.type, set())
        for field_name, field_set in _fields_by_type.items():
            if field_name == self.type:
                continue
            for f in field_set - relevant - _default_skip:
                val = getattr(self, f, None)
                if val is not None:
                    warnings.warn(
                        f"RuleEffect(type='{self.type}') has '{f}' set "
                        f"(relevant to '{field_name}' effects, ignored here)",
                        UserWarning,
                        stacklevel=2,
                    )


class RuleDefinitionBody(BaseModel):
    """Stored as definition_json in workflow_definitions for workflow_type='rule'."""

    event: RuleEvent
    when: str | None = None
    match: dict[str, Any] | None = None
    effect: RuleEffect | None = None
    effects: list[RuleEffect] | None = None
    group: str | None = None
    agent_scope: list[str] | None = None  # Only active for these agent types

    @model_validator(mode="after")
    def _validate_effects(self) -> "RuleDefinitionBody":
        has_effect = self.effect is not None
        has_effects = self.effects is not None and len(self.effects) > 0
        if has_effect and has_effects:
            raise ValueError("Specify either 'effect' or 'effects', not both")
        if not has_effect and not has_effects:
            raise ValueError("Specify either 'effect' or 'effects'")
        if has_effects:
            block_count = sum(e.type == "block" for e in self.effects)  # type: ignore[union-attr]
            if block_count > 1:
                raise ValueError("At most one 'block' effect is allowed per rule")
        return self

    @property
    def resolved_effects(self) -> list[RuleEffect]:
        """Return the canonical list of effects (works for both singular and plural)."""
        if self.effects:
            return self.effects
        if self.effect:
            return [self.effect]
        return []


class VariableDefinitionBody(BaseModel):
    """Stored as definition_json in workflow_definitions for workflow_type='variable'."""

    variable: str  # variable name
    value: Any  # default value
    description: str | None = None


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
            defaults = {"mode": "inherit", "isolation": "inherit", "provider": "inherit"}
            for field, default in defaults.items():
                if field in data and data[field] == "":
                    data[field] = default
        return data

    name: str
    description: str | None = None
    extends: str | None = None
    sources: list[str] | None = None  # Session sources this agent applies to (None = all)
    # Structured prompt fields (composed into preamble at spawn time)
    role: str | None = None
    goal: str | None = None
    personality: str | None = None
    instructions: str | None = None
    # Execution
    provider: str = "inherit"
    model: str | None = None
    mode: Literal["terminal", "embedded", "headless", "self", "inherit"] = "inherit"
    isolation: Literal["none", "worktree", "clone", "inherit"] | None = "inherit"
    base_branch: str = "inherit"
    timeout: float = 0
    max_turns: int = 0
    # Orchestration
    workflows: AgentWorkflows = Field(default_factory=AgentWorkflows)
    enabled: bool = True

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


class Observer(BaseModel):
    """Observer that watches events and sets variables.

    Two variants (exactly one must be specified):
    1. YAML observer: on + set (match optional) — inline event/variable mapping
    2. Behavior ref: behavior — references a registered behavior by name
    """

    name: str
    # YAML observer fields
    on: str | None = None  # Event type to observe (e.g., "after_tool")
    match: dict[str, str] | None = None  # Optional filter (tool, mcp_server, mcp_tool)
    set: dict[str, str] | None = None  # Variable assignments (name -> expression)
    # Behavior ref field
    behavior: str | None = None  # Registered behavior name

    def model_post_init(self, __context: Any) -> None:
        """Validate that exactly one variant is specified."""
        is_yaml = self.on is not None or self.match is not None or self.set is not None
        is_behavior = self.behavior is not None
        if is_yaml and is_behavior:
            raise ValueError(
                "Observer must specify exactly one variant: "
                "YAML observer (on/match/set) or behavior ref (behavior), not both."
            )
        if not is_yaml and not is_behavior:
            raise ValueError(
                "Observer must specify exactly one variant: "
                "YAML observer (on/match/set) or behavior ref (behavior)."
            )


class WorkflowRule(BaseModel):
    name: str | None = None
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
    model_config = ConfigDict(extra="allow")


class PrematureStopHandler(BaseModel):
    """Handler for when an agent attempts to stop before task completion."""

    action: Literal["guide_continuation", "block", "warn"] = "guide_continuation"
    message: str = (
        "Task has incomplete subtasks. Options: "
        "1) Continue: use suggest_next_task() to find the next task. "
        "2) Stop anyway: use `/g workflows deactivate` to end the workflow first."
    )
    condition: str | None = None  # Optional condition to check (e.g., task_tree_complete)


class WorkflowStep(BaseModel):
    name: str
    description: str | None = None
    status_message: str | None = (
        None  # Template rendered after on_enter, returned as system_message
    )

    on_enter: list[dict[str, Any]] = Field(default_factory=list)

    # "all" or list of tool names
    allowed_tools: list[str] | Literal["all"] = Field(default="all")
    blocked_tools: list[str] = Field(default_factory=list)

    # MCP-level tool restrictions (for call_tool arguments)
    # Format: "server:tool" (e.g., "gobby-tasks:list_tasks") or "server:*" for all tools on server
    allowed_mcp_tools: list[str] | Literal["all"] = Field(default="all")
    blocked_mcp_tools: list[str] = Field(default_factory=list)

    rules: list[WorkflowRule] = Field(default_factory=list)
    transitions: list[WorkflowTransition] = Field(default_factory=list)
    exit_when: str | None = None  # Expression shorthand AND-ed with exit_conditions
    exit_conditions: list[dict[str, Any] | str] = Field(default_factory=list)

    on_exit: list[dict[str, Any]] = Field(default_factory=list)

    # MCP tool success/error handlers - execute actions when specific MCP tools complete
    # Each handler: {server: str, tool: str, action: str, ...action_params}
    on_mcp_success: list[dict[str, Any]] = Field(default_factory=list)
    on_mcp_error: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowDefinition(BaseModel):
    name: str
    description: str | None = None
    version: str = "1.0"
    # Deprecated: use `enabled` instead. Kept for backward-compat YAML loading.
    type: Literal["lifecycle", "step"] = "step"
    extends: str | None = None

    # Instance defaults: control whether workflow starts enabled and its evaluation priority
    enabled: bool = True
    priority: int = 100

    @field_validator("version", mode="before")
    @classmethod
    def coerce_version_to_string(cls, v: Any) -> str:
        """Accept numeric versions (1.0, 2) and coerce to string."""
        return str(v) if v is not None else "1.0"

    sources: list[str] | None = None  # Session sources this workflow applies to (None = all)

    settings: dict[str, Any] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)
    # Session-scoped shared variables (visible to all workflows in the session)
    session_variables: dict[str, Any] = Field(default_factory=dict)

    # Named rule definitions (file-local)
    rule_definitions: dict[str, RuleDefinition] = Field(default_factory=dict)
    # Cross-file rule imports (e.g., ["worker-safety"])
    imports: list[str] = Field(default_factory=list)

    # Top-level tool blocking rules (same format as block_tools action rules).
    # Evaluated on BEFORE_TOOL events before trigger-based block_tools actions.
    tool_rules: list[dict[str, Any]] = Field(default_factory=list)

    # Observers: watch events and set variables or invoke registered behaviors
    observers: list[Observer] = Field(default_factory=list)

    steps: list[WorkflowStep] = Field(default_factory=list)

    # Global triggers (on_session_start, etc.)
    triggers: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)

    # Handler for premature stop attempts (step workflows only)
    # Triggered when agent tries to stop but exit_condition is not met
    on_premature_stop: PrematureStopHandler | None = None

    # Exit condition for the entire workflow (when this is true, workflow can end)
    exit_condition: str | None = None

    def get_step(self, step_name: str) -> WorkflowStep | None:
        for s in self.steps:
            if s.name == step_name:
                return s
        return None


# --- Pipeline Definition Models (YAML) ---


class WebhookEndpoint(BaseModel):
    """Configuration for a webhook endpoint."""

    url: str
    method: str = "POST"
    headers: dict[str, str] = Field(default_factory=dict)


class WebhookConfig(BaseModel):
    """Webhook configuration for pipeline events."""

    on_approval_pending: WebhookEndpoint | None = None
    on_complete: WebhookEndpoint | None = None
    on_failure: WebhookEndpoint | None = None


class PipelineApproval(BaseModel):
    """Approval gate configuration for a pipeline step."""

    required: bool = False
    message: str | None = None
    timeout_seconds: int | None = None


class MCPStepConfig(BaseModel):
    """Configuration for an MCP tool call step in a pipeline."""

    server: str
    tool: str
    arguments: dict[str, Any] | None = None


class PipelineStep(BaseModel):
    """A single step in a pipeline workflow.

    Steps must have exactly one execution type: exec, prompt, invoke_pipeline, or mcp.
    """

    id: str

    # Execution types (mutually exclusive - exactly one required)
    exec: str | None = None  # Shell command to run
    prompt: str | None = None  # LLM prompt template
    invoke_pipeline: str | dict[str, Any] | None = None  # Name of pipeline to invoke
    mcp: MCPStepConfig | None = None  # Call MCP tool directly
    activate_workflow: dict[str, Any] | None = None  # Activate workflow on a session

    # Optional fields
    condition: str | None = None  # Condition for step execution
    approval: PipelineApproval | None = None  # Approval gate
    tools: list[str] = Field(default_factory=list)  # Tool restrictions for prompt steps
    input: str | None = None  # Explicit input reference (e.g., $prev_step.output)

    def model_post_init(self, __context: Any) -> None:
        """Validate that exactly one execution type is specified."""
        exec_types = [
            self.exec,
            self.prompt,
            self.invoke_pipeline,
            self.mcp,
            self.activate_workflow,
        ]
        specified = [t for t in exec_types if t is not None]

        if len(specified) == 0:
            raise ValueError(
                "PipelineStep requires at least one execution type: "
                "exec, prompt, invoke_pipeline, mcp, or activate_workflow"
            )
        if len(specified) > 1:
            raise ValueError(
                "PipelineStep exec, prompt, invoke_pipeline, mcp, "
                "and activate_workflow are mutually exclusive - only one allowed"
            )


class PipelineDefinition(BaseModel):
    """Definition for a pipeline workflow with typed data flow between steps.

    Pipelines execute steps sequentially with explicit data flow via $step.output references.
    """

    name: str
    description: str | None = None
    version: str = "1.0"
    type: Literal["pipeline"] = "pipeline"

    @field_validator("version", mode="before")
    @classmethod
    def coerce_version_to_string(cls, v: Any) -> str:
        """Accept numeric versions (1.0, 2) and coerce to string."""
        return str(v) if v is not None else "1.0"

    # Input/output schema
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)

    # Pipeline steps
    steps: list[PipelineStep] = Field(default_factory=list)

    # Webhook notifications
    webhooks: WebhookConfig | None = None

    # Expose as MCP tool
    expose_as_tool: bool = False

    @field_validator("steps", mode="after")
    @classmethod
    def validate_steps(cls, v: list[PipelineStep]) -> list[PipelineStep]:
        """Validate pipeline steps."""
        if len(v) == 0:
            raise ValueError("Pipeline requires at least one step")

        # Check for duplicate step IDs
        ids = [step.id for step in v]
        if len(ids) != len(set(ids)):
            duplicates = [id for id in ids if ids.count(id) > 1]
            raise ValueError(f"Pipeline step IDs must be unique. Duplicates: {set(duplicates)}")

        return v

    def get_step(self, step_id: str) -> PipelineStep | None:
        """Get a step by its ID."""
        for step in self.steps:
            if step.id == step_id:
                return step
        return None


# --- Workflow State Models (Runtime) ---


class WorkflowInstance(BaseModel):
    """Represents a single workflow instance bound to a session.

    Supports multiple concurrent workflows per session. Each instance
    has its own scoped variables and step state, keyed by
    UNIQUE(session_id, workflow_name).
    """

    id: str
    session_id: str
    workflow_name: str
    enabled: bool = True
    priority: int = 100
    current_step: str | None = None
    step_entered_at: datetime | None = None
    step_action_count: int = 0
    total_action_count: int = 0
    variables: dict[str, Any] = Field(default_factory=dict)
    context_injected: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dictionary with ISO-formatted datetimes."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "workflow_name": self.workflow_name,
            "enabled": self.enabled,
            "priority": self.priority,
            "current_step": self.current_step,
            "step_entered_at": self.step_entered_at.isoformat() if self.step_entered_at else None,
            "step_action_count": self.step_action_count,
            "total_action_count": self.total_action_count,
            "variables": self.variables,
            "context_injected": self.context_injected,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowInstance":
        """Deserialize from a dictionary, parsing ISO datetime strings."""
        parsed = dict(data)
        for field in ("step_entered_at", "created_at", "updated_at"):
            val = parsed.get(field)
            if isinstance(val, str):
                parsed[field] = datetime.fromisoformat(val)
        return cls(**parsed)


class WorkflowState(BaseModel):
    session_id: str
    workflow_name: str
    step: str
    step_entered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    step_action_count: int = 0
    total_action_count: int = 0

    context_injected: bool = False

    variables: dict[str, Any] = Field(default_factory=dict)

    # Task decomposition state
    task_list: list[dict[str, Any]] | None = None
    current_task_index: int = 0
    files_modified_this_task: int = 0

    # Approval state for user_approval exit conditions
    approval_pending: bool = False
    approval_condition_id: str | None = None  # Which condition is awaiting approval
    approval_prompt: str | None = None  # The prompt shown to user
    approval_requested_at: datetime | None = None
    approval_timeout_seconds: int | None = None  # None = no timeout

    # Escape hatch: temporarily disable enforcement
    disabled: bool = False
    disabled_reason: str | None = None

    # Track initial step for reset functionality
    initial_step: str | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
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

    def to_rule_definition_body(self) -> RuleDefinitionBody:
        """Convert inline agent rule_definition to RuleDefinitionBody for rule engine."""
        effect = RuleEffect(
            type=self.action,
            reason=self.reason,
            tools=self.tools or None,
            mcp_tools=self.mcp_tools or None,
            command_pattern=self.command_pattern,
            command_not_pattern=self.command_not_pattern,
        )
        return RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            effect=effect,
        )


class RuleTriggerEvent(str, Enum):
    """Events that workflow rules can respond to."""

    TURN_START = "turn_start"
    TURN_END = "turn_end"

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    BEFORE_AGENT = "before_agent"
    AFTER_AGENT = "after_agent"
    STOP = "stop"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    BEFORE_TOOL_SELECTION = "before_tool_selection"
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    PRE_COMPACT = "pre_compact"
    POST_COMPACT = "post_compact"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_STOP = "subagent_stop"
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_DENIED = "permission_denied"
    NOTIFICATION = "notification"
    STOP_FAILURE = "stop_failure"
    TASK_CREATED = "task_created"
    TASK_COMPLETED = "task_completed"
    TEAMMATE_IDLE = "teammate_idle"
    INSTRUCTIONS_LOADED = "instructions_loaded"
    CONFIG_CHANGE = "config_change"
    CWD_CHANGED = "cwd_changed"
    FILE_CHANGED = "file_changed"
    WORKTREE_CREATE = "worktree_create"
    WORKTREE_REMOVE = "worktree_remove"
    ELICITATION = "elicitation"
    ELICITATION_RESULT = "elicitation_result"


# Backward-compatible alias for older imports. This still resolves to the
# unified rule trigger enum; there is no longer a separate filtered rule enum.
RuleEvent = RuleTriggerEvent


class RuleEffect(BaseModel):
    """What happens when a rule fires."""

    type: Literal[
        "block",
        "set_variable",
        "inject_context",
        "mcp_call",
        "observe",
        "rewrite_input",
        "set_permission_response",
        "set_retry",
        "set_watch_paths",
        "set_worktree_path",
        "set_elicitation",
        "load_skill",
    ]

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
    inject_result: bool = False  # Capture result and inject as agent context
    block_on_failure: bool = False  # Block original tool call if this mcp_call fails
    block_on_success: bool = False  # Block original tool call if this mcp_call succeeds

    # observe — append structured entry to _observations session variable
    category: str | None = None
    message: str | None = None

    # rewrite_input — modify tool input before execution (PreToolUse)
    input_updates: dict[str, Any] | None = None
    auto_approve: bool = False
    permission_decision: Literal["allow", "deny"] | None = None
    updated_permissions: list[dict[str, Any]] | None = None

    # set_retry — tell Claude an auto-denied tool may be retried
    retry: bool = False

    # set_watch_paths — update dynamic FileChanged watchers
    watch_paths: list[str] | None = None

    # set_worktree_path — override the created worktree directory
    worktree_path: str | None = None

    # set_elicitation — programmatically answer or override elicitation results
    elicitation_action: Literal["accept", "decline", "cancel"] | None = None
    elicitation_content: dict[str, Any] | None = None
    elicitation_error: str | None = None

    # load_skill — emit an on-demand skill fetch directive into agent context
    skill: str | None = None

    def model_post_init(self, __context: Any) -> None:
        """Warn when fields irrelevant to the effect type are set."""
        import warnings

        selector_fields = {"tools", "mcp_tools", "command_pattern", "command_not_pattern"}
        _fields_by_type: dict[str, set[str]] = {
            "block": {"reason", *selector_fields},
            "set_variable": {"variable", "value"},
            "inject_context": {"template", *selector_fields},
            "mcp_call": {
                "server",
                "tool",
                "arguments",
                "background",
                "inject_result",
                "block_on_failure",
                "block_on_success",
                *selector_fields,
            },
            "observe": {"category", "message", *selector_fields},
            "rewrite_input": {"input_updates", "auto_approve", *selector_fields},
            "set_permission_response": {
                "permission_decision",
                "input_updates",
                "updated_permissions",
                *selector_fields,
            },
            "set_retry": {"retry", *selector_fields},
            "set_watch_paths": {"watch_paths", *selector_fields},
            "set_worktree_path": {"worktree_path", *selector_fields},
            "set_elicitation": {
                "elicitation_action",
                "elicitation_content",
                "elicitation_error",
                *selector_fields,
            },
            "load_skill": {"skill", *selector_fields},
        }
        # Fields with non-None defaults that shouldn't trigger warnings
        _default_skip = {
            "background",
            "when",
            "auto_approve",
            "inject_result",
            "block_on_failure",
            "block_on_success",
            "message",
            "retry",
        }
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

    event: RuleTriggerEvent
    when: str | None = None
    match: dict[str, Any] | None = None
    tools: list[str] | None = None  # Pre-filter: skip rule if tool doesn't match
    effects: list[RuleEffect] | None = None
    group: str | None = None
    agent_scope: list[str] | None = None  # Only active for these agent types

    @model_validator(mode="after")
    def _validate_effects(self) -> RuleDefinitionBody:
        if not self.effects or len(self.effects) == 0:
            raise ValueError("'effects' is required and must be non-empty")
        block_count = sum(e.type == "block" for e in self.effects)
        if block_count > 1:
            raise ValueError("At most one 'block' effect is allowed per rule")
        return self

    @property
    def resolved_effects(self) -> list[RuleEffect]:
        """Return the canonical list of effects."""
        return self.effects or []


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
    model_config = ConfigDict(extra="ignore")  # Tolerate stale YAML with mode: field

    isolation: Literal["none", "worktree", "clone", "inherit"] | None = "inherit"
    base_branch: str = "inherit"
    timeout: float = 0
    max_turns: int = 0
    # Orchestration
    workflows: AgentWorkflows = Field(default_factory=AgentWorkflows)
    enabled: bool = True
    deprecated: bool = False
    deprecated_reason: str | None = None
    skills: dict[str, list[str]] = Field(default_factory=dict)
    # Agent-level tool restrictions (applied regardless of step workflow)
    blocked_tools: list[str] = Field(default_factory=list)
    blocked_mcp_tools: list[str] = Field(default_factory=list)
    # Inline step workflow (replaces external step workflow YAML files)
    steps: list[WorkflowStep] | None = None
    step_variables: dict[str, Any] = Field(default_factory=dict)
    exit_condition: str | None = None

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


class WorkflowTransition(BaseModel):
    """Transition between workflow steps."""

    to: str
    when: str
    on_transition: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowStep(BaseModel):
    """A single step in a step workflow with tool enforcement."""

    name: str
    description: str | None = None
    status_message: str | None = None

    on_enter: list[dict[str, Any]] = Field(default_factory=list)

    # "all" or list of tool names
    allowed_tools: list[str] | Literal["all"] = Field(default="all")
    blocked_tools: list[str] = Field(default_factory=list)

    # MCP-level tool restrictions: "server:tool" or "server:*"
    allowed_mcp_tools: list[str] | Literal["all"] = Field(default="all")
    blocked_mcp_tools: list[str] = Field(default_factory=list)

    transitions: list[WorkflowTransition] = Field(default_factory=list)
    exit_when: str | None = None

    on_exit: list[dict[str, Any]] = Field(default_factory=list)

    # MCP tool success/error handlers — execute actions when specific MCP tools complete
    # Each handler: {server: str, tool: str, action: str, ...action_params}
    on_mcp_success: list[dict[str, Any]] = Field(default_factory=list)
    on_mcp_error: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowDefinition(BaseModel):
    name: str
    description: str | None = None
    type: str = "step"
    version: str = "1.0"
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

    # Observers: watch events and set variables or invoke registered behaviors
    observers: list[Observer] = Field(default_factory=list)

    # Inline tool blocking rules for lifecycle workflows
    tool_rules: list[dict[str, Any]] = Field(default_factory=list)

    # Step workflow steps (empty for rule-only workflows)
    steps: list[WorkflowStep] = Field(default_factory=list)

    # Exit condition for the entire workflow
    exit_condition: str | None = None

    def get_step(self, step_name: str) -> WorkflowStep | None:
        """Get a step by name."""
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

    Steps must have exactly one execution type: exec, prompt, invoke_pipeline, mcp, or wait.
    """

    id: str

    # Execution types (mutually exclusive - exactly one required)
    exec: str | None = None  # Shell command to run
    prompt: str | None = None  # LLM prompt template
    invoke_pipeline: str | dict[str, Any] | None = None  # Name of pipeline to invoke
    mcp: MCPStepConfig | None = None  # Call MCP tool directly
    activate_workflow: dict[str, Any] | None = None  # Activate workflow on session
    wait: dict[str, Any] | None = None  # Block until completion event fires

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
            self.wait,
        ]
        specified = [t for t in exec_types if t is not None]

        if len(specified) == 0:
            raise ValueError(
                "PipelineStep requires at least one execution type: "
                "exec, prompt, invoke_pipeline, mcp, activate_workflow, or wait"
            )
        if len(specified) > 1:
            raise ValueError(
                "PipelineStep exec, prompt, invoke_pipeline, mcp, activate_workflow, "
                "and wait are mutually exclusive - only one allowed"
            )


class PipelineDefinition(BaseModel):
    """Definition for a pipeline workflow with typed data flow between steps.

    Pipelines execute steps sequentially with explicit data flow via $step.output references.
    """

    name: str
    description: str | None = None
    version: str = "1.0"
    type: Literal["pipeline"] = "pipeline"
    enabled: bool = True
    priority: int = 100
    deprecated: bool = False
    deprecated_reason: str | None = None

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

    # Resume execution after daemon restart (opt-in, steps must be idempotent)
    resume_on_restart: bool = False

    @field_validator("steps", mode="after")
    @classmethod
    def validate_steps(cls, v: list[PipelineStep]) -> list[PipelineStep]:
        """Validate pipeline steps."""
        ids = [step.id for step in v]
        if len(ids) != len(set(ids)):
            duplicates = [id for id in ids if ids.count(id) > 1]
            raise ValueError(f"Pipeline step IDs must be unique. Duplicates: {set(duplicates)}")

        return v

    @model_validator(mode="after")
    def validate_active_pipeline_has_steps(self) -> PipelineDefinition:
        """Allow empty steps only for disabled deprecated tombstones."""
        if self.steps:
            return self
        if self.enabled is False and self.deprecated is True and self.deprecated_reason:
            return self
        raise ValueError("Pipeline requires at least one step")

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
    def from_dict(cls, data: dict[str, Any]) -> WorkflowInstance:
        """Deserialize from a dictionary, parsing ISO datetime strings."""
        parsed = dict(data)
        for field in ("step_entered_at", "created_at", "updated_at"):
            val = parsed.get(field)
            if isinstance(val, str):
                parsed[field] = datetime.fromisoformat(val)
        return cls(**parsed)

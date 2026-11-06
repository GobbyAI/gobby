from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from gobby.workflows.agent_models import (
        AgentDefinitionBody as AgentDefinitionBody,
    )
    from gobby.workflows.agent_models import (
        AgentPromptBlocks as AgentPromptBlocks,
    )
    from gobby.workflows.agent_models import (
        AgentSelector as AgentSelector,
    )
    from gobby.workflows.agent_models import (
        AgentStepWorkflowBody as AgentStepWorkflowBody,
    )
    from gobby.workflows.agent_models import (
        AgentWorkflows as AgentWorkflows,
    )
    from gobby.workflows.pipeline_models import (
        MCPStepConfig as MCPStepConfig,
    )
    from gobby.workflows.pipeline_models import (
        PipelineApproval as PipelineApproval,
    )
    from gobby.workflows.pipeline_models import (
        PipelineDefinition as PipelineDefinition,
    )
    from gobby.workflows.pipeline_models import (
        PipelineStep as PipelineStep,
    )
    from gobby.workflows.pipeline_models import (
        WebhookConfig as WebhookConfig,
    )
    from gobby.workflows.pipeline_models import (
        WebhookEndpoint as WebhookEndpoint,
    )

# --- Workflow Definition Models (YAML) ---

SUPPORTED_WORKFLOW_DEFINITION_TYPES = frozenset({"agent", "pipeline", "rule", "variable"})
_WORKFLOW_ENABLED_ADAPTER = TypeAdapter(bool)


def normalize_workflow_definition_enabled(
    data: dict[str, Any],
    *,
    default: bool = True,
) -> bool:
    """Return the Pydantic-normalized enabled value for definition metadata."""
    return _WORKFLOW_ENABLED_ADAPTER.validate_python(data.get("enabled", default))


def validate_skill_script_path(script: str) -> None:
    """Validate a skill script path before materialization or execution."""
    posix = PurePosixPath(script)
    windows = PureWindowsPath(script)
    if not script.strip():
        raise ValueError("Skill script path must be non-empty")
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError("Skill script path must be relative")
    if ".." in posix.parts or ".." in windows.parts:
        raise ValueError("Skill script path cannot traverse its scripts directory")


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
            effects=[effect],
        )


class RuleTriggerEvent(str, Enum):
    """Events that workflow rules can respond to."""

    TURN_START = "turn_start"
    TURN_END = "turn_end"

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    SETUP = "setup"
    BEFORE_AGENT = "before_agent"
    AFTER_AGENT = "after_agent"
    STOP = "stop"
    USER_PROMPT_EXPANSION = "user_prompt_expansion"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    BEFORE_TOOL_SELECTION = "before_tool_selection"
    POST_TOOL_BATCH = "post_tool_batch"
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    PRE_COMPACT = "pre_compact"
    POST_COMPACT = "post_compact"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_STOP = "subagent_stop"
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_DENIED = "permission_denied"
    NOTIFICATION = "notification"
    MESSAGE_DISPLAY = "message_display"
    DIRECTORY_ADDED = "directory_added"
    STOP_FAILURE = "stop_failure"
    TASK_CREATED = "task_created"
    TASK_CLOSE = "task_close"
    TASK_LAND = "task_land"
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


class RuleEffect(BaseModel):
    """What happens when a rule fires."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "block",
        "set_variable",
        "inject_context",
        "set_display_content",
        "mcp_call",
        "observe",
        "rewrite_input",
        "set_permission_response",
        "set_retry",
        "set_watch_paths",
        "set_worktree_path",
        "set_elicitation",
        "load_skill",
        "run_command",
        "proxy_hook",
    ]

    # Per-effect condition (gates this individual effect within a multi-effect rule)
    when: str | None = None
    # `on_receipt` stages with the payload it suppresses; `eager` persists now.
    delivery: Literal["eager", "on_receipt"] = "eager"

    # block — prevent the action
    reason: str | None = None
    # Pure expression over the next tool's event data and current variables.
    recovery_when: str | None = None
    acknowledge_variable: str | None = Field(
        default=None, json_schema_extra={"variable_writer_for": "block"}
    )
    tools: list[str] | None = None
    mcp_tools: list[str] | None = None
    command_pattern: str | None = None
    command_not_pattern: str | None = None
    # Blank quoted-string data before matching command_pattern and
    # command_not_pattern, so prose inside commit messages or echo arguments
    # cannot sit in command position. Double-quoted spans containing `$(` or a
    # backtick stay visible: command substitution in them still executes.
    mask_quoted: bool = False
    # Match command_pattern against the command `uv run …` executes. A rule
    # whose required form is `uv run` turns this off.
    resolve_uv_run: bool = True

    # set_variable — update session/workflow state
    variable: str | None = Field(
        default=None, json_schema_extra={"variable_writer_for": "set_variable"}
    )
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
    success_variable: str | None = Field(
        default=None, json_schema_extra={"variable_writer_for": "mcp_call"}
    )  # Set to true after successful inline dispatch

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

    # run_command — spawn a local command with the hook event JSON on stdin.
    # Fail-open by design: missing executable/script, non-zero exit, timeout, or
    # unparseable output never blocks the event. Reuses background/inject_result.
    command: list[str] | None = None
    script: str | None = None
    timeout_seconds: float | None = None

    # proxy_hook — trusted synchronous input transformation resolved internally.
    handler: str | None = None

    @classmethod
    def variable_write_targets(cls, effect: Mapping[str, Any]) -> list[str]:
        """Enumerate target names from the writer fields declared on this model.

        Accept raw mappings so mutation admission also protects legacy or malformed
        stored definitions without requiring them to pass current validation.
        """
        targets = []
        for name, field in cls.model_fields.items():
            metadata = field.json_schema_extra
            if isinstance(metadata, dict) and metadata.get("variable_writer_for") == effect.get(
                "type"
            ):
                target = effect.get(name)
                if isinstance(target, str):
                    targets.append(target)
        return targets

    @model_validator(mode="after")
    def _validate_required_fields(self) -> RuleEffect:
        required_fields: dict[str, tuple[str, ...]] = {
            "block": ("reason",),
            "set_variable": ("variable",),
            "inject_context": ("template",),
            "set_display_content": ("template",),
            "mcp_call": ("server", "tool"),
            "rewrite_input": ("input_updates",),
            "set_watch_paths": ("watch_paths",),
            "set_worktree_path": ("worktree_path",),
            "load_skill": ("skill",),
            "run_command": ("command",),
            "proxy_hook": ("handler",),
        }
        missing = [
            field_name
            for field_name in required_fields.get(self.type, ())
            if getattr(self, field_name) is None
        ]
        if missing:
            fields = ", ".join(missing)
            raise ValueError(f"RuleEffect(type='{self.type}') requires: {fields}")

        if self.type in {"mcp_call", "run_command"}:
            if self.timeout_seconds is not None and not self.timeout_seconds > 0:
                raise ValueError(
                    f"RuleEffect(type='{self.type}') timeout_seconds must be > 0 "
                    f"(got {self.timeout_seconds!r})"
                )

        if self.type == "run_command":
            if not self.command:
                raise ValueError("RuleEffect(type='run_command') requires a non-empty command")
            if (self.skill is None) != (self.script is None):
                raise ValueError("run_command skill and script must be provided together")
            if self.skill is not None and not self.skill.strip():
                raise ValueError("run_command skill must be non-empty")
            if self.script is not None:
                validate_skill_script_path(self.script)
        if self.type == "proxy_hook":
            if self.timeout_seconds is not None and not self.timeout_seconds > 0:
                raise ValueError(
                    "RuleEffect(type='proxy_hook') timeout_seconds must be > 0 "
                    f"(got {self.timeout_seconds!r})"
                )
            forbidden = {
                "background": self.background,
                "command": self.command,
                "script": self.script,
                "skill": self.skill,
                "template": self.template,
                "server": self.server,
                "tool": self.tool,
                "arguments": self.arguments,
                "inject_result": self.inject_result,
                "permission_decision": self.permission_decision,
                "input_updates": self.input_updates,
                "auto_approve": self.auto_approve,
                "updated_permissions": self.updated_permissions,
            }
            configured = [name for name, value in forbidden.items() if value]
            if configured:
                raise ValueError(
                    "RuleEffect(type='proxy_hook') forbids: " + ", ".join(sorted(configured))
                )

        if self.success_variable is not None and (not self.inject_result or self.background):
            raise ValueError("mcp_call success_variable requires inline result injection")

        if self.type == "set_permission_response" and all(
            value is None
            for value in (
                self.permission_decision,
                self.input_updates,
                self.updated_permissions,
            )
        ):
            raise ValueError(
                "RuleEffect(type='set_permission_response') requires at least one of: "
                "permission_decision, input_updates, updated_permissions"
            )

        if self.type == "set_elicitation" and all(
            value is None
            for value in (
                self.elicitation_action,
                self.elicitation_content,
                self.elicitation_error,
            )
        ):
            raise ValueError(
                "RuleEffect(type='set_elicitation') requires at least one of: "
                "elicitation_action, elicitation_content, elicitation_error"
            )

        return self

    def model_post_init(self, __context: Any) -> None:
        """Warn when fields irrelevant to the effect type are set."""
        import warnings

        # Post-init precedes after-model validators. Reject this invalid contract
        # before advisory irrelevant-field warnings can obscure its ValidationError.
        if self.recovery_when is not None and self.type != "block":
            raise ValueError("recovery_when is only valid on block effects")

        if self.type == "proxy_hook":
            # The strict proxy validator emits one actionable error for unsafe fields.
            return

        selector_fields = {"tools", "mcp_tools", "command_pattern", "command_not_pattern"}
        _fields_by_type: dict[str, set[str]] = {
            "block": {"reason", "recovery_when", "acknowledge_variable", *selector_fields},
            "set_variable": {"variable", "value", *selector_fields},
            "inject_context": {"template", *selector_fields},
            "set_display_content": {"template", *selector_fields},
            "mcp_call": {
                "server",
                "tool",
                "arguments",
                "background",
                "inject_result",
                "block_on_failure",
                "block_on_success",
                "success_variable",
                "timeout_seconds",
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
            "run_command": {
                "command",
                "skill",
                "script",
                "timeout_seconds",
                "background",
                "inject_result",
                *selector_fields,
            },
            "proxy_hook": {"handler", "timeout_seconds", *selector_fields},
        }
        # Fields with non-None defaults that shouldn't trigger warnings
        _default_skip = {
            "background",
            "when",
            "delivery",
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


class AttestationReferenceShape(BaseModel):
    """The evidence reference a closure rule can accept."""

    model_config = ConfigDict(extra="forbid")

    scheme: str = Field(min_length=1)
    value_pattern: str = Field(min_length=1)

    @field_validator("value_pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"value_pattern is not a valid regex: {exc}") from exc
        return value


class AttestationShape(BaseModel):
    """What a closure rule can accept for these kinds, enforced when evidence is recorded."""

    model_config = ConfigDict(extra="forbid")

    kinds: list[str] = Field(min_length=1)
    result: str | None = Field(default=None, min_length=1)
    reference: AttestationReferenceShape | None = None
    # Expression over `task` alone; the shape applies to every task when omitted.
    applies_when: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _constrains(self) -> AttestationShape:
        if self.result is None and self.reference is None:
            raise ValueError("attestation_shape must declare a result or reference")
        if self.applies_when is not None:
            from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

            try:
                SafeExpressionEvaluator.validate_syntax(
                    self.applies_when, set(ATTESTATION_SHAPE_FUNCTIONS)
                )
            except (SyntaxError, ValueError) as exc:
                raise ValueError(f"applies_when is not a valid expression: {exc}") from exc
        return self


ATTESTATION_SHAPE_FUNCTIONS: dict[str, Callable[..., Any]] = {"len": len, "any": any, "all": all}


class RuleDefinitionBody(BaseModel):
    """Stored as definition_json on rule_definitions."""

    model_config = ConfigDict(extra="forbid")

    event: RuleTriggerEvent
    when: str | None = None
    match: dict[str, Any] | None = None
    tools: list[str] | None = None  # Pre-filter: skip rule if tool doesn't match
    effects: list[RuleEffect] | None = None
    group: str | None = None
    audience: str | None = None  # all, interactive, autonomous, or a concrete audience name
    agent_scope: list[str] | None = None  # Only active for these agent types
    # Active for every definition; no rule_selectors include or exclude removes it.
    mandatory: bool = False
    # Seats cannot toggle, edit or delete a protected rule; the operator principal still can.
    protected: bool = False
    # Same-tool retry escalation: evaluated first on before_tool; its block preempts the rest.
    retry_limit: bool = False
    # Closure rules only: record_attestation refuses evidence this shape can never accept.
    attestation_shape: AttestationShape | None = None

    @model_validator(mode="after")
    def _validate_effects(self) -> RuleDefinitionBody:
        if not self.effects or len(self.effects) == 0:
            raise ValueError("'effects' is required and must be non-empty")
        if self.attestation_shape is not None and self.event not in {
            RuleTriggerEvent.TASK_CLOSE,
            RuleTriggerEvent.TASK_LAND,
        }:
            raise ValueError("attestation_shape is restricted to task_close and task_land rules")
        if self.retry_limit and self.event != RuleTriggerEvent.BEFORE_TOOL:
            raise ValueError("retry_limit is restricted to before_tool rules")
        block_count = sum(e.type == "block" for e in self.effects)
        if block_count > 1:
            raise ValueError("At most one 'block' effect is allowed per rule")
        if any(effect.type == "proxy_hook" for effect in self.effects):
            if self.event != RuleTriggerEvent.BEFORE_TOOL:
                raise ValueError("proxy_hook effects are restricted to before_tool rules")
        if any(effect.recovery_when is not None for effect in self.effects):
            if self.event != RuleTriggerEvent.BEFORE_TOOL:
                raise ValueError("recovery_when is restricted to before_tool rules")
        if self.event in {RuleTriggerEvent.TASK_CLOSE, RuleTriggerEvent.TASK_LAND}:
            if any(
                value is not None
                for value in (self.match, self.tools, self.agent_scope, self.audience)
            ):
                raise ValueError(
                    f"{self.event.value} rules declare applicability in snapshot expressions"
                )
            for effect in self.effects:
                if effect.type != "block" or set(effect.model_dump(exclude_defaults=True)) - {
                    "type",
                    "reason",
                    "when",
                }:
                    raise ValueError(
                        f"{self.event.value} effects allow only block, reason and when"
                    )
        return self

    @property
    def resolved_effects(self) -> list[RuleEffect]:
        """Return the canonical list of effects."""
        return self.effects or []


RULE_DEFINITION_ROW_METADATA_FIELDS = (
    "name",
    "description",
    "enabled",
    "priority",
    "tags",
)


class RuleDefinitionMetadata(BaseModel):
    """Validated row-level metadata for a rule definition."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    enabled: bool = True
    priority: int = 100
    tags: list[str] = Field(default_factory=list)


def split_rule_definition_data(
    data: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate and validate a stored rule body from row-level metadata."""
    body_data = dict(data)
    name = body_data.pop("name", None)
    raw_metadata = {
        field: body_data.pop(field)
        for field in RULE_DEFINITION_ROW_METADATA_FIELDS
        if field in body_data
    }

    RuleDefinitionBody.model_validate(body_data)
    metadata = RuleDefinitionMetadata.model_validate(raw_metadata).model_dump(exclude_unset=True)
    if name is not None:
        metadata["name"] = name
    return body_data, metadata


class VariableDefinitionBody(BaseModel):
    """Stored as definition_json on session_variable_defaults."""

    variable: str  # variable name
    value: Any  # default value
    description: str | None = None


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


class SeatMessageProtocol(BaseModel):
    """YAML-owned message syntax and step routing for receipt-driven handlers."""

    model_config = ConfigDict(extra="forbid")

    report_prefixes: list[str] | None = None
    report_step: str | None = None
    consensus_pattern: str | None = None
    consensus_step: str | None = None
    withdrawal_step: str | None = None
    consensus_event: str | None = None
    withdrawal_event: str | None = None

    @model_validator(mode="after")
    def validate_protocol(self) -> SeatMessageProtocol:
        reporting = self.report_prefixes is not None or self.report_step is not None
        if reporting and (
            not self.report_prefixes or not all(self.report_prefixes) or not self.report_step
        ):
            raise ValueError("Reporting requires nonempty report_prefixes and report_step")
        consensus = (
            self.consensus_pattern,
            self.consensus_step,
            self.withdrawal_step,
            self.consensus_event,
            self.withdrawal_event,
        )
        if any(value is not None for value in consensus):
            if not all(consensus):
                raise ValueError("Consensus requires a pattern, steps and event identifiers")
            try:
                pattern = re.compile(self.consensus_pattern or "")
            except re.error as exc:
                raise ValueError(f"Invalid consensus_pattern: {exc}") from exc
            if pattern.groups < 2:
                raise ValueError("consensus_pattern must capture an event and a commit SHA")
        elif not reporting:
            raise ValueError("A message protocol must declare reporting or consensus behavior")
        return self


class WorkflowStep(BaseModel):
    """A single step in a step workflow with tool enforcement."""

    name: str
    description: str | None = None
    status_message: str | None = None
    # A standing seat's idle step: the seat may stop here once it declares seat_idle.
    idle: bool = False

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

    # MCP tool handlers — execute actions when specific MCP tools are attempted
    # or complete.
    # Each handler: {server: str, tool: str, action: str, ...action_params}
    on_mcp_before: list[dict[str, Any]] = Field(default_factory=list)
    on_mcp_success: list[dict[str, Any]] = Field(default_factory=list)
    on_mcp_error: list[dict[str, Any]] = Field(default_factory=list)
    # Explicit fallback: an unhandled MCP failure leaves the workflow on this step.
    mcp_error_policy: Literal["stay"] | None = None

    @field_validator("on_mcp_before", "on_mcp_success", "on_mcp_error")
    @classmethod
    def validate_message_protocols(cls, handlers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for handler in handlers:
            if handler.get("action") == "advance_seat":
                SeatMessageProtocol.model_validate(handler.get("protocol"))
        return handlers


class WorkflowDefinition(BaseModel):
    name: str
    description: str | None = None
    type: str = "step"
    version: str = "1.0"
    extends: str | None = None

    # Instance defaults: control whether workflow starts enabled and its evaluation priority
    enabled: bool = True
    priority: int = 100
    deprecated: bool = False
    deprecated_reason: str | None = None

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


def validate_workflow_definition_data(
    data: dict[str, Any],
    *,
    expected_type: str | None = None,
) -> str:
    """Validate creatable workflow YAML and return its effective definition type.

    Generic create/import operations require an explicit supported type. Updates may omit
    ``type`` and are validated using the persisted row type, but may never change that type.
    """
    declared_type = data.get("type")
    effective_type = expected_type if expected_type is not None else declared_type

    if (
        not isinstance(effective_type, str)
        or effective_type not in SUPPORTED_WORKFLOW_DEFINITION_TYPES
    ):
        supported = ", ".join(sorted(SUPPORTED_WORKFLOW_DEFINITION_TYPES))
        raise ValueError(
            f"Invalid or missing 'type' in YAML: {effective_type!r}. Must be one of: {supported}."
        )

    if declared_type is not None and declared_type != effective_type:
        raise ValueError(
            f"Invalid type {declared_type!r}: does not match existing workflow type "
            f"{effective_type!r}"
        )

    if effective_type == "pipeline":
        from gobby.workflows.pipeline_models import PipelineDefinition

        PipelineDefinition.model_validate(data)
        return effective_type

    common_metadata = {"description", "enabled", "name", "priority", "sources", "type", "version"}
    if effective_type == "rule":
        body = {key: value for key, value in data.items() if key not in common_metadata}
        RuleDefinitionBody.model_validate(body, extra="forbid")
    elif effective_type == "variable":
        variable_metadata = common_metadata - {"description"}
        body = {key: value for key, value in data.items() if key not in variable_metadata}
        VariableDefinitionBody.model_validate(body, extra="forbid")
    else:
        from gobby.workflows.agent_models import AgentDefinitionBody

        agent_metadata = {"priority", "tags", "type", "version"}
        body = {key: value for key, value in data.items() if key not in agent_metadata}
        AgentDefinitionBody.model_validate(body, extra="forbid")

    return effective_type


_AGENT_MODEL_EXPORTS = frozenset(
    {
        "AgentDefinitionBody",
        "AgentPromptBlocks",
        "AgentSelector",
        "AgentStepWorkflowBody",
        "AgentWorkflows",
    }
)
_PIPELINE_MODEL_EXPORTS = frozenset(
    {
        "MCPStepConfig",
        "PipelineApproval",
        "PipelineDefinition",
        "PipelineStep",
        "WebhookConfig",
        "WebhookEndpoint",
    }
)


def __getattr__(name: str) -> Any:
    """Permanently re-export split agent and pipeline models."""
    if name in _AGENT_MODEL_EXPORTS:
        from gobby.workflows import agent_models

        value = getattr(agent_models, name)
        globals()[name] = value
        return value
    if name in _PIPELINE_MODEL_EXPORTS:
        from gobby.workflows import pipeline_models

        value = getattr(pipeline_models, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _AGENT_MODEL_EXPORTS | _PIPELINE_MODEL_EXPORTS)

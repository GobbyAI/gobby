"""Executable provider capability declarations for CLI hook adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from gobby.adapters.agy_contract import AGY_HOOK_ALIASES, AGY_HOOK_CONTRACTS
from gobby.adapters.claude_contract import CLAUDE_HOOK_CONTRACTS
from gobby.adapters.droid_contract import DROID_HOOK_CONTRACTS
from gobby.adapters.qwen_contract import QWEN_HOOK_CONTRACTS
from gobby.hooks.events import HookEventType, HookResponse, SessionSource


class ContextChannel(StrEnum):
    """Native channel used for model-visible context injection."""

    ADDITIONAL_CONTEXT = "additionalContext"
    SYSTEM_MESSAGE = "systemMessage"
    NONE = "none"


class ProviderDecisionStyle(StrEnum):
    """Provider response-control style for one native hook event."""

    NONE = "none"
    TOP_LEVEL_BLOCK = "top_level_block"
    PRE_TOOL_USE = "pre_tool_use"
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_DENIED = "permission_denied"
    HARD_STOP = "hard_stop"
    WATCH_PATHS = "watch_paths"
    WORKTREE_CREATE = "worktree_create"
    ELICITATION = "elicitation"
    ELICITATION_RESULT = "elicitation_result"
    COMPACT_STOP = "compact_stop"
    MODEL_REQUEST = "model_request"
    TOOL_SELECTION = "tool_selection"
    IGNORE_BLOCK = "ignore_block"
    DISPLAY_CONTENT = "display_content"


class ReasonFormat(StrEnum):
    """Provider-specific reason shaping applied at the adapter boundary."""

    PASSTHROUGH = "passthrough"


RESPONSE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "decision",
        "context",
        "system_message",
        "reason",
        "display_content",
        "modified_input",
        "auto_approve",
        "permission_decision",
        "updated_permissions",
        "retry",
        "watch_paths",
        "worktree_path",
        "elicitation_action",
        "elicitation_content",
        "elicitation_error",
        "modify_args",
        "trigger_action",
        "metadata",
    }
)
BASE_SUPPORTED_RESPONSE_FIELDS: frozenset[str] = frozenset(
    {"decision", "system_message", "reason", "metadata"}
)
TransportCapabilityValue = bool | tuple[str, ...]


@dataclass(frozen=True)
class HookCapability:
    """Native response capability for one provider hook event."""

    hook_name: str
    event_type: HookEventType
    decision_style: ProviderDecisionStyle
    context_channel: ContextChannel = ContextChannel.NONE
    reason_format: ReasonFormat = ReasonFormat.PASSTHROUGH
    supported_response_fields: frozenset[str] = field(default_factory=frozenset)

    @property
    def unsupported_response_fields(self) -> frozenset[str]:
        """HookResponse fields with no native destination for this hook."""
        return RESPONSE_FIELD_NAMES - self.supported_response_fields

    def supports_response_field(self, field_name: str) -> bool:
        """Return whether this hook has a native or Gobby-mediated destination."""
        return field_name in self.supported_response_fields


@dataclass(frozen=True)
class ProviderCapabilities:
    """Capability registry row for one CLI/provider source."""

    source: SessionSource
    hook_events: Mapping[str, HookCapability]
    hook_aliases: Mapping[str, str] = field(default_factory=dict)
    transport_capabilities: Mapping[str, TransportCapabilityValue] = field(default_factory=dict)
    supports_permissions: bool = False
    supports_permission_neutral_rewrite: bool = False
    supports_elicitation: bool = False

    def get_hook(self, hook_type: str | None) -> HookCapability | None:
        """Resolve a native hook name or adapter alias to its capability."""
        if not hook_type:
            return None
        if hook_type in self.hook_events:
            return self.hook_events[hook_type]
        alias = self.hook_aliases.get(hook_type)
        if alias:
            return self.hook_events.get(alias)
        return None

    def context_channel_for(self, hook_type: str | None) -> ContextChannel:
        """Return context routing for a hook, defaulting to no native channel."""
        capability = self.get_hook(hook_type)
        return capability.context_channel if capability else ContextChannel.NONE


def _response_fields(
    *field_names: str,
    context_channel: ContextChannel = ContextChannel.NONE,
) -> frozenset[str]:
    fields = set(BASE_SUPPORTED_RESPONSE_FIELDS)
    if context_channel is not ContextChannel.NONE:
        fields.add("context")
    fields.update(field_names)
    return frozenset(fields)


def _claude_capabilities() -> ProviderCapabilities:
    events: dict[str, HookCapability] = {}
    aliases: dict[str, str] = {}
    for contract in CLAUDE_HOOK_CONTRACTS:
        context_channel = (
            ContextChannel.ADDITIONAL_CONTEXT
            if contract.allows_additional_context
            else ContextChannel.NONE
        )
        extra_fields: list[str] = []
        if contract.decision_style.value == ProviderDecisionStyle.PRE_TOOL_USE:
            extra_fields.extend(["permission_decision", "auto_approve", "modified_input"])
        elif contract.decision_style.value == ProviderDecisionStyle.PERMISSION_REQUEST:
            extra_fields.extend(
                [
                    "permission_decision",
                    "auto_approve",
                    "modified_input",
                    "updated_permissions",
                ]
            )
        elif contract.decision_style.value == ProviderDecisionStyle.PERMISSION_DENIED:
            extra_fields.append("retry")
        elif contract.decision_style.value == ProviderDecisionStyle.WATCH_PATHS:
            extra_fields.append("watch_paths")
        elif contract.decision_style.value == ProviderDecisionStyle.WORKTREE_CREATE:
            extra_fields.append("worktree_path")
        elif contract.decision_style.value == ProviderDecisionStyle.ELICITATION:
            extra_fields.extend(["elicitation_action", "elicitation_content", "elicitation_error"])
        elif contract.decision_style.value == ProviderDecisionStyle.ELICITATION_RESULT:
            extra_fields.extend(["elicitation_action", "elicitation_content"])
        elif contract.decision_style.value == ProviderDecisionStyle.DISPLAY_CONTENT:
            extra_fields.append("display_content")

        events[contract.native_name] = HookCapability(
            hook_name=contract.native_name,
            event_type=contract.event_type,
            decision_style=ProviderDecisionStyle(contract.decision_style.value),
            context_channel=context_channel,
            reason_format=ReasonFormat.PASSTHROUGH,
            supported_response_fields=_response_fields(
                *extra_fields,
                context_channel=context_channel,
            ),
        )
        aliases[contract.hook_event_name] = contract.native_name

    return ProviderCapabilities(
        source=SessionSource.CLAUDE,
        hook_events=events,
        hook_aliases=aliases,
        supports_permissions=True,
        supports_permission_neutral_rewrite=True,
        supports_elicitation=True,
    )


def _qwen_capabilities() -> ProviderCapabilities:
    events: dict[str, HookCapability] = {}
    for contract in QWEN_HOOK_CONTRACTS:
        context_channel = (
            ContextChannel.ADDITIONAL_CONTEXT
            if contract.allows_additional_context
            else ContextChannel.NONE
        )
        extra_fields: list[str] = []
        if contract.decision_style.value == ProviderDecisionStyle.PRE_TOOL_USE:
            extra_fields.extend(["permission_decision", "auto_approve", "modified_input"])
        elif contract.decision_style.value == ProviderDecisionStyle.PERMISSION_REQUEST:
            extra_fields.extend(
                [
                    "permission_decision",
                    "auto_approve",
                    "modified_input",
                    "updated_permissions",
                ]
            )

        events[contract.native_name] = HookCapability(
            hook_name=contract.native_name,
            event_type=contract.event_type,
            decision_style=ProviderDecisionStyle(contract.decision_style.value),
            context_channel=context_channel,
            supported_response_fields=_response_fields(
                *extra_fields,
                context_channel=context_channel,
            ),
        )

    return ProviderCapabilities(
        source=SessionSource.QWEN,
        hook_events=events,
        supports_permissions=True,
        supports_permission_neutral_rewrite=True,
    )


ACP_EVENT_MAP: dict[str, HookEventType] = {
    "SessionStart": HookEventType.SESSION_START,
    "SessionEnd": HookEventType.SESSION_END,
    "BeforeAgent": HookEventType.BEFORE_AGENT,
    "AfterAgent": HookEventType.AFTER_AGENT,
    "BeforeTool": HookEventType.BEFORE_TOOL,
    "AfterTool": HookEventType.AFTER_TOOL,
    "BeforeToolSelection": HookEventType.BEFORE_TOOL_SELECTION,
    "BeforeModel": HookEventType.BEFORE_MODEL,
    "AfterModel": HookEventType.AFTER_MODEL,
    "PreCompress": HookEventType.PRE_COMPACT,
    "Notification": HookEventType.NOTIFICATION,
}

ACP_HOOK_ALIASES: dict[str, str] = {
    "session_start": "SessionStart",
    "session_end": "SessionEnd",
    "before_agent": "BeforeAgent",
    "after_agent": "AfterAgent",
    "before_tool": "BeforeTool",
    "after_tool": "AfterTool",
    "before_tool_selection": "BeforeToolSelection",
    "before_model": "BeforeModel",
    "after_model": "AfterModel",
    "pre_compact": "PreCompress",
    "notification": "Notification",
}

ACP_ADDITIONAL_CONTEXT_HOOKS = frozenset({"SessionStart", "BeforeAgent", "BeforeTool", "AfterTool"})


def _acp_capabilities(source: SessionSource) -> ProviderCapabilities:
    events: dict[str, HookCapability] = {}
    for hook_name, event_type in ACP_EVENT_MAP.items():
        context_channel = (
            ContextChannel.ADDITIONAL_CONTEXT
            if hook_name in ACP_ADDITIONAL_CONTEXT_HOOKS
            else ContextChannel.NONE
        )
        decision_style = ProviderDecisionStyle.TOP_LEVEL_BLOCK
        extra_fields: list[str] = []
        if hook_name == "BeforeModel":
            decision_style = ProviderDecisionStyle.MODEL_REQUEST
            extra_fields.append("modify_args")
        elif hook_name == "BeforeToolSelection":
            decision_style = ProviderDecisionStyle.TOOL_SELECTION
            extra_fields.append("modify_args")
        events[hook_name] = HookCapability(
            hook_name=hook_name,
            event_type=event_type,
            decision_style=decision_style,
            context_channel=context_channel,
            supported_response_fields=_response_fields(
                *extra_fields,
                context_channel=context_channel,
            ),
        )

    return ProviderCapabilities(
        source=source,
        hook_events=events,
        hook_aliases=ACP_HOOK_ALIASES,
    )


CODEX_EVENT_MAP: dict[str, HookEventType] = {
    "SessionStart": HookEventType.SESSION_START,
    "SubagentStart": HookEventType.SUBAGENT_START,
    "UserPromptSubmit": HookEventType.BEFORE_AGENT,
    "PreToolUse": HookEventType.BEFORE_TOOL,
    "PermissionRequest": HookEventType.PERMISSION_REQUEST,
    "PostToolUse": HookEventType.AFTER_TOOL,
    "PreCompact": HookEventType.PRE_COMPACT,
    "PostCompact": HookEventType.POST_COMPACT,
    "SubagentStop": HookEventType.SUBAGENT_STOP,
    "Stop": HookEventType.STOP,
    "SessionEnd": HookEventType.SESSION_END,
}

CODEX_ADDITIONAL_CONTEXT_HOOKS = frozenset(
    {"SessionStart", "SubagentStart", "UserPromptSubmit", "PostToolUse"}
)
CODEX_SYSTEM_MESSAGE_CONTEXT_HOOKS = frozenset(
    {"PreToolUse", "PermissionRequest", "PreCompact", "PostCompact", "Stop"}
)


def _codex_capabilities() -> ProviderCapabilities:
    events: dict[str, HookCapability] = {}
    for hook_name, event_type in CODEX_EVENT_MAP.items():
        if hook_name in CODEX_ADDITIONAL_CONTEXT_HOOKS:
            context_channel = ContextChannel.ADDITIONAL_CONTEXT
        elif hook_name in CODEX_SYSTEM_MESSAGE_CONTEXT_HOOKS:
            context_channel = ContextChannel.SYSTEM_MESSAGE
        else:
            context_channel = ContextChannel.NONE

        decision_style = ProviderDecisionStyle.TOP_LEVEL_BLOCK
        extra_fields: list[str] = []
        if hook_name == "PreToolUse":
            decision_style = ProviderDecisionStyle.PRE_TOOL_USE
            extra_fields.extend(["permission_decision", "auto_approve", "modified_input"])
        elif hook_name == "PermissionRequest":
            decision_style = ProviderDecisionStyle.PERMISSION_REQUEST
            extra_fields.extend(["permission_decision", "auto_approve"])
        elif hook_name in {"PreCompact", "PostCompact"}:
            decision_style = ProviderDecisionStyle.COMPACT_STOP
        elif hook_name == "Stop":
            decision_style = ProviderDecisionStyle.HARD_STOP
        elif hook_name in {"SessionStart", "SubagentStart", "UserPromptSubmit", "PostToolUse"}:
            decision_style = ProviderDecisionStyle.NONE

        events[hook_name] = HookCapability(
            hook_name=hook_name,
            event_type=event_type,
            decision_style=decision_style,
            context_channel=context_channel,
            supported_response_fields=_response_fields(
                *extra_fields,
                context_channel=context_channel,
            ),
        )
    return ProviderCapabilities(
        source=SessionSource.CODEX,
        hook_events=events,
        supports_permissions=True,
        supports_permission_neutral_rewrite=True,
    )


GROK_EVENT_MAP: dict[str, HookEventType] = {
    "session_start": HookEventType.SESSION_START,
    "session_end": HookEventType.SESSION_END,
    "user_prompt_submit": HookEventType.BEFORE_AGENT,
    "pre_tool_use": HookEventType.BEFORE_TOOL,
    "post_tool_use": HookEventType.AFTER_TOOL,
    "post_tool_use_failure": HookEventType.AFTER_TOOL,
    "pre_compact": HookEventType.PRE_COMPACT,
    "post_compact": HookEventType.POST_COMPACT,
    "stop": HookEventType.STOP,
    "notification": HookEventType.NOTIFICATION,
    "permission_denied": HookEventType.PERMISSION_DENIED,
    "stop_failure": HookEventType.STOP_FAILURE,
    "subagent_start": HookEventType.SUBAGENT_START,
    "subagent_stop": HookEventType.SUBAGENT_STOP,
}

GROK_HOOK_ALIASES: dict[str, str] = {
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit",
    "PreToolUse": "pre_tool_use",
    "PostToolUse": "post_tool_use",
    "PostToolUseFailure": "post_tool_use_failure",
    "PreCompact": "pre_compact",
    "PostCompact": "post_compact",
    "Stop": "stop",
    "Notification": "notification",
    "PermissionDenied": "permission_denied",
    "StopFailure": "stop_failure",
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
    "SubagentEnd": "subagent_stop",
    "subagent_end": "subagent_stop",
}

GROK_ADDITIONAL_CONTEXT_HOOKS = frozenset({"stop", "subagent_stop"})
GROK_TRANSPORT_CAPABILITIES: dict[str, TransportCapabilityValue] = {
    "loadSession": True,
    "x.ai/fs_notify": True,
    "cancelRewind": True,
    "availableCommands": ("compact", "context", "session-info"),
}


def _grok_capabilities() -> ProviderCapabilities:
    events: dict[str, HookCapability] = {}
    for hook_name, event_type in GROK_EVENT_MAP.items():
        if hook_name in GROK_ADDITIONAL_CONTEXT_HOOKS:
            context_channel = ContextChannel.ADDITIONAL_CONTEXT
        else:
            context_channel = ContextChannel.NONE

        decision_style = ProviderDecisionStyle.NONE
        extra_fields: list[str] = []
        if hook_name == "pre_tool_use":
            decision_style = ProviderDecisionStyle.PRE_TOOL_USE
            extra_fields.extend(["permission_decision", "auto_approve", "modified_input"])
        elif hook_name in {"stop", "subagent_stop"}:
            decision_style = ProviderDecisionStyle.TOP_LEVEL_BLOCK

        events[hook_name] = HookCapability(
            hook_name=hook_name,
            event_type=event_type,
            decision_style=decision_style,
            context_channel=context_channel,
            supported_response_fields=_response_fields(
                *extra_fields,
                context_channel=context_channel,
            ),
        )

    return ProviderCapabilities(
        source=SessionSource.GROK,
        hook_events=events,
        hook_aliases=GROK_HOOK_ALIASES,
        transport_capabilities=GROK_TRANSPORT_CAPABILITIES,
        supports_permissions=True,
        supports_permission_neutral_rewrite=True,
    )


def _unsupported_capabilities(source: SessionSource) -> ProviderCapabilities:
    return ProviderCapabilities(source=source, hook_events={})


def _agy_capabilities() -> ProviderCapabilities:
    events: dict[str, HookCapability] = {}
    for hook_name, contract in AGY_HOOK_CONTRACTS.items():
        decision_style = ProviderDecisionStyle.TOP_LEVEL_BLOCK
        extra_fields: list[str] = []
        if contract.blocks_tool_call:
            decision_style = ProviderDecisionStyle.PRE_TOOL_USE
            extra_fields.extend(["permission_decision", "auto_approve", "modified_input"])
        elif hook_name == "Stop":
            decision_style = ProviderDecisionStyle.HARD_STOP

        events[hook_name] = HookCapability(
            hook_name=hook_name,
            event_type=contract.event_type,
            decision_style=decision_style,
            context_channel=ContextChannel.NONE,
            supported_response_fields=_response_fields(*extra_fields),
        )

    return ProviderCapabilities(
        source=SessionSource.AGY,
        hook_events=events,
        hook_aliases=AGY_HOOK_ALIASES,
        supports_permissions=True,
        supports_permission_neutral_rewrite=True,
    )


def _droid_capabilities() -> ProviderCapabilities:
    events: dict[str, HookCapability] = {}
    for hook_name, contract in DROID_HOOK_CONTRACTS.items():
        context_channel = (
            ContextChannel.ADDITIONAL_CONTEXT
            if contract.allows_additional_context
            else ContextChannel.NONE
        )
        extra_fields: list[str] = []
        if contract.decision_style.value == ProviderDecisionStyle.PRE_TOOL_USE:
            extra_fields.extend(["permission_decision", "auto_approve", "modified_input"])
        events[hook_name] = HookCapability(
            hook_name=hook_name,
            event_type=contract.event_type,
            decision_style=ProviderDecisionStyle(contract.decision_style.value),
            context_channel=context_channel,
            supported_response_fields=_response_fields(
                *extra_fields,
                context_channel=context_channel,
            ),
        )
    return ProviderCapabilities(
        source=SessionSource.DROID,
        hook_events=events,
        supports_permissions=True,
        supports_permission_neutral_rewrite=True,
    )


PROVIDER_CAPABILITIES: dict[SessionSource, ProviderCapabilities] = {
    SessionSource.AGY: _agy_capabilities(),
    SessionSource.CLAUDE: _claude_capabilities(),
    SessionSource.CODEX: _codex_capabilities(),
    SessionSource.GROK: _grok_capabilities(),
    SessionSource.QWEN: _qwen_capabilities(),
    SessionSource.DROID: _droid_capabilities(),
    SessionSource.UNKNOWN: _unsupported_capabilities(SessionSource.UNKNOWN),
}

SOURCE_ALIASES: dict[str, SessionSource] = {
    "claude_code": SessionSource.CLAUDE,
    "claude": SessionSource.CLAUDE,
    "codex": SessionSource.CODEX,
    "grok": SessionSource.GROK,
    "qwen": SessionSource.QWEN,
    "droid": SessionSource.DROID,
    "agy": SessionSource.AGY,
    "unknown": SessionSource.UNKNOWN,
}


def normalize_source(source: SessionSource | str) -> SessionSource:
    """Normalize source strings used by HTTP envelopes and HookEvent objects."""
    if isinstance(source, SessionSource):
        return source
    try:
        return SOURCE_ALIASES[source]
    except KeyError as exc:
        raise ValueError(f"Unknown adapter source: {source!r}") from exc


def get_provider_capabilities(source: SessionSource | str) -> ProviderCapabilities:
    """Return executable capabilities for a current adapter provider."""
    normalized = normalize_source(source)
    return PROVIDER_CAPABILITIES[normalized]


def _response_field_present(response: HookResponse, field_name: str) -> bool:
    value: Any = getattr(response, field_name)
    if value is None:
        return False
    if value is False:
        return False
    if value == "":
        return False
    if isinstance(value, (dict, list, tuple, set, frozenset)) and not value:
        return False
    return True


def present_unsupported_response_fields(
    response: HookResponse,
    capability: HookCapability | None,
) -> tuple[str, ...]:
    """Return populated HookResponse fields unsupported by a hook capability."""
    unsupported_fields = (
        capability.unsupported_response_fields if capability is not None else RESPONSE_FIELD_NAMES
    )
    return tuple(
        field_name
        for field_name in sorted(unsupported_fields)
        if _response_field_present(response, field_name)
    )

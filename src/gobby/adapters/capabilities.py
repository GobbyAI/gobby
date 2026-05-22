"""Executable provider capability declarations for CLI hook adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from gobby.adapters.claude_contract import CLAUDE_HOOK_CONTRACTS
from gobby.adapters.droid_contract import DROID_HOOK_CONTRACTS
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


class ReasonFormat(StrEnum):
    """Provider-specific reason shaping applied at the adapter boundary."""

    PASSTHROUGH = "passthrough"
    CLAUDE_PRE_TOOL_COMPACT = "claude_pre_tool_compact"


RESPONSE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "decision",
        "context",
        "system_message",
        "reason",
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
    supports_permissions: bool = False
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

        reason_format = (
            ReasonFormat.CLAUDE_PRE_TOOL_COMPACT
            if contract.native_name == "pre-tool-use"
            else ReasonFormat.PASSTHROUGH
        )
        events[contract.native_name] = HookCapability(
            hook_name=contract.native_name,
            event_type=contract.event_type,
            decision_style=ProviderDecisionStyle(contract.decision_style.value),
            context_channel=context_channel,
            reason_format=reason_format,
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
        supports_elicitation=True,
    )


GEMINI_EVENT_MAP: dict[str, HookEventType] = {
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

GEMINI_HOOK_ALIASES: dict[str, str] = {
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

GEMINI_CONTEXT_HOOKS = frozenset(GEMINI_EVENT_MAP)


def _gemini_like_capabilities(source: SessionSource) -> ProviderCapabilities:
    events: dict[str, HookCapability] = {}
    for hook_name, event_type in GEMINI_EVENT_MAP.items():
        context_channel = (
            ContextChannel.ADDITIONAL_CONTEXT
            if hook_name in GEMINI_CONTEXT_HOOKS
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
        hook_aliases=GEMINI_HOOK_ALIASES,
    )


CODEX_EVENT_MAP: dict[str, HookEventType] = {
    "SessionStart": HookEventType.SESSION_START,
    "UserPromptSubmit": HookEventType.BEFORE_AGENT,
    "PreToolUse": HookEventType.BEFORE_TOOL,
    "PermissionRequest": HookEventType.PERMISSION_REQUEST,
    "PostToolUse": HookEventType.AFTER_TOOL,
    "PreCompact": HookEventType.PRE_COMPACT,
    "PostCompact": HookEventType.POST_COMPACT,
    "Stop": HookEventType.STOP,
}

CODEX_ADDITIONAL_CONTEXT_HOOKS = frozenset({"SessionStart", "UserPromptSubmit", "PostToolUse"})
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
        elif hook_name in {"SessionStart", "UserPromptSubmit", "PostToolUse"}:
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
    )


PROVIDER_CAPABILITIES: dict[SessionSource, ProviderCapabilities] = {
    SessionSource.CLAUDE: _claude_capabilities(),
    SessionSource.CODEX: _codex_capabilities(),
    SessionSource.GEMINI: _gemini_like_capabilities(SessionSource.GEMINI),
    SessionSource.QWEN: _gemini_like_capabilities(SessionSource.QWEN),
    SessionSource.DROID: _droid_capabilities(),
}

SOURCE_ALIASES: dict[str, SessionSource] = {
    "claude_code": SessionSource.CLAUDE,
    "claude": SessionSource.CLAUDE,
    "codex": SessionSource.CODEX,
    "gemini": SessionSource.GEMINI,
    "qwen": SessionSource.QWEN,
    "droid": SessionSource.DROID,
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
    if capability is None:
        return ()
    return tuple(
        field_name
        for field_name in sorted(capability.unsupported_response_fields)
        if _response_field_present(response, field_name)
    )

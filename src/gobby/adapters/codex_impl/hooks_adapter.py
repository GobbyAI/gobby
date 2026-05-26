"""Codex hooks.json adapter implementation."""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.adapters.base import (
    BaseAdapter,
    build_first_hook_session_metadata_lines,
    normalize_adapter_response_reason,
    system_message_has_session_banner,
)
from gobby.adapters.capabilities import (
    CODEX_EVENT_MAP,
    ContextChannel,
    get_provider_capabilities,
)
from gobby.adapters.codex_impl.shared import (
    TOOL_MAP as SHARED_TOOL_MAP,
)
from gobby.adapters.degradation import (
    AdapterDegradationKind,
    record_adapter_degradation,
    record_unsupported_response_fields,
    truncate_context_for_adapter,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.llm.sdk_utils import ADDITIONAL_CONTEXT_LIMIT

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

logger = logging.getLogger(__name__)

_CONTEXT_SEPARATOR = "\n\n"
_TRUNCATION_MARKER = "\n... [truncated]"


def _bound_context_parts(
    context_parts: list[tuple[str, str]],
) -> tuple[str, dict[str, int], list[tuple[str, int, int]]]:
    """Join context parts without letting one oversized part erase later context."""
    bounded_parts: list[tuple[str, str]] = []
    contributor_sizes: dict[str, int] = {}
    trimmed_parts: list[tuple[str, int, int]] = []
    used = 0

    for label, part in context_parts:
        separator_len = len(_CONTEXT_SEPARATOR) if bounded_parts else 0
        remaining = ADDITIONAL_CONTEXT_LIMIT - used - separator_len
        if remaining <= 0:
            trimmed_parts.append((label, len(part), 0))
            continue

        bounded_part = part
        if len(part) > remaining:
            if remaining > len(_TRUNCATION_MARKER):
                bounded_part = part[: remaining - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
            else:
                bounded_part = part[:remaining]
            trimmed_parts.append((label, len(part), len(bounded_part)))

        bounded_parts.append((label, bounded_part))
        contributor_sizes[label] = len(part)
        used += separator_len + len(bounded_part)

    return (
        _CONTEXT_SEPARATOR.join(part for _, part in bounded_parts),
        contributor_sizes,
        trimmed_parts,
    )


class CodexHooksAdapter(BaseAdapter):
    """Adapter for Codex CLI hooks.json lifecycle events.

    Translates Codex hooks.json payloads to unified HookEvent format and
    converts HookResponse back to the event-specific JSON schema Codex expects
    on hook stdout.
    """

    source = SessionSource.CODEX

    # Event type mapping: Codex PascalCase hook names -> unified HookEventType
    EVENT_MAP: dict[str, HookEventType] = dict(CODEX_EVENT_MAP)

    # Hook events where context must be routed through top-level systemMessage.
    # These schemas do not support hookSpecificOutput.additionalContext.
    SYSTEM_MESSAGE_ONLY_EVENTS: set[str] = {
        "PreToolUse",
        "PermissionRequest",
        "PreCompact",
        "PostCompact",
        "Stop",
    }
    COMPACT_EVENTS: set[str] = {"PreCompact", "PostCompact"}

    def __init__(self, hook_manager: HookManager | None = None):
        self._hook_manager = hook_manager

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent | None:
        """Convert Codex hooks.json payload to HookEvent."""
        hook_type = native_event.get("hook_type", "")
        input_data = native_event.get("input_data") or {}

        event_type = self.EVENT_MAP.get(hook_type)
        if event_type is None:
            logger.warning(f"Codex hooks: unsupported hook type '{hook_type}'")
            return None

        session_id = input_data.get("session_id", "")
        raw_tool_input = input_data.get("tool_input")

        # Normalize event data (same as Claude — reuse shared normalization)
        from gobby.hooks.normalization import normalize_tool_fields

        normalized_data = normalize_tool_fields(dict(input_data))
        raw_tool_name = normalized_data.get("tool_name")
        if isinstance(raw_tool_name, str):
            normalized_tool_name = SHARED_TOOL_MAP.get(raw_tool_name, raw_tool_name)
            if normalized_tool_name != raw_tool_name:
                normalized_data.setdefault("_original_tool_name", raw_tool_name)
                normalized_data["tool_name"] = normalized_tool_name

        # Check for failure on PostToolUse
        is_failure = normalized_data.get("is_error", False)
        metadata = {"is_failure": is_failure} if is_failure else {}
        if isinstance(raw_tool_input, dict):
            metadata["raw_tool_input"] = copy.deepcopy(raw_tool_input)
        original_tool_name = normalized_data.pop("_original_tool_name", None)
        if original_tool_name:
            metadata["original_tool_name"] = original_tool_name
            metadata["normalized_tool_name"] = normalized_data.get("tool_name")
        self._copy_platform_session_metadata(native_event, metadata)

        return HookEvent(
            event_type=event_type,
            session_id=session_id,
            source=self.source,
            timestamp=datetime.now(UTC),
            machine_id=input_data.get("machine_id"),
            cwd=input_data.get("cwd"),
            data=normalized_data,
            metadata=metadata,
        )

    def translate_from_hook_response(
        self, response: HookResponse, hook_type: str | None = None
    ) -> dict[str, Any]:
        """Convert HookResponse to Codex hooks.json expected format."""
        hook_event_name = hook_type or "Unknown"
        capabilities = get_provider_capabilities(self.source)
        capability = capabilities.get_hook(hook_event_name)
        context_channel = capability.context_channel if capability else ContextChannel.NONE
        record_unsupported_response_fields(
            response,
            provider=self.source,
            hook_type=hook_type,
            capability=capability,
            event_logger=logger,
        )
        normalized_reason = normalize_adapter_response_reason(
            response,
            adapter_name=self.__class__.__name__,
            hook_type=hook_type,
            logger=logger,
        )

        result: dict[str, Any] = {"continue": True}

        if response.decision in ("deny", "block"):
            if hook_event_name == "PermissionRequest":
                decision: dict[str, Any] = {"behavior": "deny"}
                if normalized_reason:
                    decision["message"] = normalized_reason
                result["hookSpecificOutput"] = {
                    "hookEventName": "PermissionRequest",
                    "decision": decision,
                }

            elif hook_event_name in self.COMPACT_EVENTS:
                return {
                    "continue": False,
                    "stopReason": normalized_reason or "Blocked by Gobby hook",
                }

            elif hook_event_name == "PreToolUse":
                deny_result: dict[str, Any] = {
                    "decision": "block",
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                    },
                }
                if normalized_reason:
                    deny_result["reason"] = normalized_reason
                    deny_result["hookSpecificOutput"]["permissionDecisionReason"] = (
                        normalized_reason
                    )

                system_parts: list[str] = []
                if response.system_message:
                    system_parts.append(response.system_message)
                if response.context:
                    system_parts.append(response.context)
                if system_parts:
                    if response.context:
                        record_adapter_degradation(
                            provider=self.source,
                            hook_type=hook_type,
                            kind=AdapterDegradationKind.REROUTED_FIELD,
                            response_field="context",
                            destination_channel=ContextChannel.SYSTEM_MESSAGE,
                            event_logger=logger,
                        )
                    deny_result["systemMessage"] = truncate_context_for_adapter(
                        "\n\n".join(system_parts),
                        provider=self.source,
                        hook_type=hook_type,
                        destination_channel=ContextChannel.SYSTEM_MESSAGE,
                        contributor_sizes={
                            f"system_part_{idx}": len(part)
                            for idx, part in enumerate(system_parts, start=1)
                        },
                        event_logger=logger,
                    )
                return deny_result

            else:
                block_result: dict[str, Any] = {"continue": False, "decision": "block"}
                if normalized_reason:
                    block_result["reason"] = normalized_reason
                return block_result

        if hook_event_name == "PermissionRequest" and "hookSpecificOutput" not in result:
            result["hookSpecificOutput"] = {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }

        if isinstance(response.modified_input, dict) and hook_event_name == "PreToolUse":
            result["hookSpecificOutput"] = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": response.modified_input,
            }

        # Build additionalContext from all context sources. Keep high-value
        # session/system context ahead of large workflow payloads.
        context_parts: list[tuple[str, str]] = []

        session_start_hook = hook_event_name == "SessionStart"

        # Route system_message by event type:
        # - systemMessage-only events: visible systemMessage
        # - SessionStart: startup context only via additionalContext
        # - UserPromptSubmit, PostToolUse: additionalContext only (hidden from user)
        if response.system_message:
            if context_channel is ContextChannel.SYSTEM_MESSAGE:
                result["systemMessage"] = response.system_message
            else:
                # Always feed to model via additionalContext
                context_parts.insert(0, ("system_message", response.system_message))

        # Session metadata (Gobby session ID, terminal context, etc.)
        if response.metadata:
            gobby_session_id = response.metadata.get("session_id")

            if gobby_session_id:
                context_lines = build_first_hook_session_metadata_lines(
                    response.metadata,
                    include_session_id_line=not (
                        session_start_hook
                        and system_message_has_session_banner(response.system_message)
                    ),
                    include_tty=False,
                )
                if context_lines:
                    context_parts.append(("metadata", "\n".join(context_lines)))

        # Workflow-injected context (inject_context action). This can be large,
        # so place it after session-critical context before applying the budget.
        if response.context:
            context_parts.append(("response.context", response.context))

        # Build hookSpecificOutput or systemMessage based on event type.
        if context_parts:
            bounded_context, contributor_sizes, trimmed_parts = _bound_context_parts(context_parts)
            for label, original_len, bounded_len in trimmed_parts:
                record_adapter_degradation(
                    provider=self.source,
                    hook_type=hook_type,
                    kind=AdapterDegradationKind.CONTEXT_TRUNCATED,
                    response_field=label,
                    destination_channel=context_channel,
                    detail=(
                        f"bounded_part original_len={original_len} "
                        f"bounded_len={bounded_len} limit={ADDITIONAL_CONTEXT_LIMIT}"
                    ),
                    event_logger=logger,
                )
            combined_context = truncate_context_for_adapter(
                bounded_context,
                provider=self.source,
                hook_type=hook_type,
                destination_channel=context_channel,
                contributor_sizes=contributor_sizes,
                event_logger=logger,
            )
            if context_channel is ContextChannel.SYSTEM_MESSAGE:
                if response.context:
                    record_adapter_degradation(
                        provider=self.source,
                        hook_type=hook_type,
                        kind=AdapterDegradationKind.REROUTED_FIELD,
                        response_field="context",
                        destination_channel=ContextChannel.SYSTEM_MESSAGE,
                        event_logger=logger,
                    )
                # Append to existing systemMessage (from system_message routing above)
                # instead of overwriting it.
                if "systemMessage" in result:
                    result["systemMessage"] += "\n\n" + combined_context
                else:
                    result["systemMessage"] = combined_context
            elif context_channel is ContextChannel.ADDITIONAL_CONTEXT:
                hook_specific = result.get("hookSpecificOutput")
                if not isinstance(hook_specific, dict):
                    hook_specific = {"hookEventName": hook_event_name}
                    result["hookSpecificOutput"] = hook_specific
                else:
                    hook_specific.setdefault("hookEventName", hook_event_name)
                hook_specific["additionalContext"] = combined_context

        return result

    def handle_native(
        self, native_event: dict[str, Any], hook_manager: HookManager
    ) -> dict[str, Any]:
        """Process Codex hooks.json event."""
        hook_event = self.translate_to_hook_event(native_event)
        if hook_event is None:
            return {}

        hook_type = native_event.get("hook_type", "")
        hook_response = hook_manager.handle(hook_event)
        return self.translate_from_hook_response(hook_response, hook_type=hook_type)


CodexNotifyAdapter = CodexHooksAdapter

__all__ = ["CodexHooksAdapter", "CodexNotifyAdapter"]

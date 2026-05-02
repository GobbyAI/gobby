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
from gobby.adapters.codex_impl.shared import (
    TOOL_MAP as SHARED_TOOL_MAP,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

logger = logging.getLogger(__name__)


class CodexHooksAdapter(BaseAdapter):
    """Adapter for Codex CLI hooks.json lifecycle events.

    Translates Codex hooks.json payloads (SessionStart, UserPromptSubmit,
    PreToolUse, PostToolUse, Stop) to unified HookEvent format and converts
    HookResponse back to the JSON schema Codex expects on hook stdout.

    Codex hooks.json uses the same input format as Claude Code (same event
    names, same stdin JSON structure) but expects a different output schema:
    - No ``continue`` field
    - ``decision``: ``"approve"`` or ``"block"``
    - ``hookSpecificOutput.additionalContext`` for context injection
    """

    source = SessionSource.CODEX

    # Event type mapping: Codex PascalCase hook names -> unified HookEventType
    EVENT_MAP: dict[str, HookEventType] = {
        "SessionStart": HookEventType.SESSION_START,
        "UserPromptSubmit": HookEventType.BEFORE_AGENT,
        "PreToolUse": HookEventType.BEFORE_TOOL,
        "PostToolUse": HookEventType.AFTER_TOOL,
        "Stop": HookEventType.STOP,
    }

    # Hook events that only accept systemMessage (not additionalContext).
    # Codex rejects/ignores additionalContext for these event types.
    SYSTEM_MESSAGE_ONLY_EVENTS: set[str] = {"PreToolUse", "Stop"}

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
        from gobby.llm.sdk_utils import truncate_additional_context

        hook_event_name = hook_type or "Unknown"
        normalized_reason = normalize_adapter_response_reason(
            response,
            adapter_name=self.__class__.__name__,
            hook_type=hook_type,
            logger=logger,
        )

        if (
            response.modified_input
            and response.decision not in ("deny", "block")
            and hook_event_name == "PreToolUse"
        ):
            logger.debug(
                "Codex PreToolUse hook returned modified_input; Codex does not support "
                "updatedInput. Proxy will apply rewrite at dispatch via "
                "apply_before_tool_enforcement. Decision=%s.",
                response.decision or "allow",
            )

        if response.decision in ("deny", "block"):
            if hook_event_name == "PreToolUse":
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
                    deny_result["systemMessage"] = truncate_additional_context(
                        "\n\n".join(system_parts),
                        contributor_sizes={
                            f"system_part_{idx}": len(part)
                            for idx, part in enumerate(system_parts, start=1)
                        },
                        logger=logger,
                    )
                return deny_result

            block_result: dict[str, Any] = {"continue": False, "decision": "block"}
            if normalized_reason:
                block_result["reason"] = normalized_reason
            return block_result

        result: dict[str, Any] = {"continue": True}

        # Build additionalContext from all context sources
        context_parts: list[tuple[str, str]] = []

        # Workflow-injected context (inject_context action)
        if response.context:
            context_parts.append(("response.context", response.context))

        session_start_hook = hook_event_name == "SessionStart"

        # Route system_message by event type:
        # - systemMessage-only events (PreToolUse, Stop): visible systemMessage
        # - SessionStart: startup context only via additionalContext
        # - UserPromptSubmit, PostToolUse: additionalContext only (hidden from user)
        if response.system_message:
            if hook_event_name in self.SYSTEM_MESSAGE_ONLY_EVENTS:
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

        # Build hookSpecificOutput or systemMessage based on event type.
        # PreToolUse/Stop only accept systemMessage — additionalContext is rejected.
        if context_parts:
            combined_context = truncate_additional_context(
                "\n\n".join(part for _, part in context_parts),
                contributor_sizes={label: len(part) for label, part in context_parts},
                logger=logger,
            )
            if hook_event_name in self.SYSTEM_MESSAGE_ONLY_EVENTS:
                # Append to existing systemMessage (from system_message routing above)
                # instead of overwriting it.
                if "systemMessage" in result:
                    result["systemMessage"] += "\n\n" + combined_context
                else:
                    result["systemMessage"] = combined_context
            else:
                result["hookSpecificOutput"] = {
                    "hookEventName": hook_event_name,
                    "additionalContext": combined_context,
                }

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

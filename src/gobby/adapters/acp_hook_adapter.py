"""Shared ACP-style CLI adapter for hook translation.

This adapter translates between ACP-style native hook formats and the unified
HookEvent/HookResponse models.

ACP-style hook types:
- SessionStart, SessionEnd: Session lifecycle
- BeforeAgent, AfterAgent: Agent turn lifecycle
- BeforeTool, AfterTool: Tool execution lifecycle
- BeforeToolSelection: Before tool selection
- BeforeModel, AfterModel: Model call lifecycle
- PreCompress: Context compression (maps to PRE_COMPACT)
- Notification: System notifications

Key differences from Claude Code:
- Uses PascalCase hook names (SessionStart vs session-start)
- Uses `hook_event_name` field instead of `hook_type`
- Has BeforeToolSelection, BeforeModel, AfterModel (not in Claude)
- Missing PermissionRequest, SubagentStart, SubagentStop (Claude-only)
- Different tool names (RunShellCommand vs Bash)
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.adapters.acp_tool_names import ACP_TOOL_NAME_MAP, normalize_acp_tool_name
from gobby.adapters.base import (
    ADAPTER_EMPTY_BLOCK_REASON_SENTINEL,
    BaseAdapter,
    build_first_hook_session_metadata_lines,
    normalize_adapter_response_reason,
    system_message_has_session_banner,
)
from gobby.adapters.capabilities import (
    ACP_EVENT_MAP,
    ACP_HOOK_ALIASES,
    ContextChannel,
    get_provider_capabilities,
)
from gobby.adapters.degradation import (
    persist_kwargs_from_hook_response,
    record_unsupported_response_fields,
    truncate_context_for_adapter,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager


logger = logging.getLogger(__name__)


class ACPHookAdapter(BaseAdapter):
    """Adapter for ACP-style CLI hook translation.

    This adapter:
    1. Translates ACP-style PascalCase hook payloads to unified HookEvent
    2. Translates HookResponse back to the provider's expected format
    3. Calls HookManager.handle() with unified HookEvent model
    """

    @property
    def source(self) -> SessionSource:
        raise NotImplementedError("ACP hook adapters must declare a concrete source")

    # Event type mapping: ACP-style hook names -> unified HookEventType.
    # ACP-style hooks use PascalCase names in the payload's "hook_event_name" field.
    EVENT_MAP: dict[str, HookEventType] = dict(ACP_EVENT_MAP)

    # Reverse mapping for response translation
    HOOK_EVENT_NAME_MAP: dict[str, str] = dict(ACP_HOOK_ALIASES)

    # Tool name mapping: ACP-style tool names -> normalized names.
    # ACP providers use different tool names than Claude Code.
    # This enables workflows to use Claude Code naming conventions
    TOOL_MAP: dict[str, str] = ACP_TOOL_NAME_MAP

    @classmethod
    def _response_hook_event_name(cls, hook_type: str | None) -> str | None:
        """Resolve the emitted hookEventName for response payloads."""
        if not hook_type:
            return None
        if hook_type in cls.EVENT_MAP:
            return hook_type
        return cls.HOOK_EVENT_NAME_MAP.get(hook_type)

    def __init__(self, hook_manager: "HookManager | None" = None):
        """Initialize the ACP hook adapter.

        Args:
            hook_manager: Reference to HookManager for handling events.
                         If None, the adapter can only translate (not handle events).
        """
        self._hook_manager = hook_manager

    def _event_logger(self) -> logging.Logger:
        """Return the concrete adapter logger for boundary telemetry."""
        return logging.getLogger(self.__class__.__module__)

    def normalize_tool_name(self, acp_tool_name: str) -> str:
        """Normalize ACP-style tool name to standard format.

        Args:
            acp_tool_name: Tool name from an ACP-style provider.

        Returns:
            Normalized tool name (e.g., "Bash", "Read", "Write").
        """
        return self.TOOL_MAP.get(acp_tool_name, normalize_acp_tool_name(acp_tool_name))

    def _normalize_event_data(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize ACP event data for CLI-agnostic processing.

        Delegates field-alias and MCP normalization to the shared
        ``normalize_tool_fields`` helper, then applies the ACP provider
        TOOL_MAP (e.g. ``write_file`` → ``Write``).

        Args:
            input_data: Raw input data from an ACP-style provider

        Returns:
            Enriched data dict with normalized fields added
        """
        from gobby.hooks.normalization import normalize_tool_fields

        data = dict(input_data)
        normalize_tool_fields(data)

        # Provider-specific: map tool names to Claude Code conventions.
        if "tool_name" in data:
            data["tool_name"] = self.normalize_tool_name(data["tool_name"])

        # ACP-style AfterAgent hooks can expose the model reply as ``prompt_response``.
        # Normalize it so downstream transcript and hook consumers can rely on
        # the same ``response`` field used by other CLIs.
        if "prompt_response" in data and "response" not in data:
            data["response"] = data["prompt_response"]

        return data

    @staticmethod
    def _is_cancelled_after_agent(input_data: dict[str, Any]) -> bool:
        """Heuristic for ACP user-interrupt AfterAgent turns.

        Context7 docs show normal AfterAgent hooks expose ``prompt_response``.
        When a provider fires AfterAgent without any response payload, treat that
        as a cancelled/interrupted turn and stop the loop instead of retrying
        a block forever.
        """
        response = input_data.get("prompt_response") or input_data.get("response")
        if response is None:
            return True
        if isinstance(response, str):
            return response.strip() == ""
        return False

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent:
        """Convert an ACP-style native event to unified HookEvent.

        ACP-style payloads have the structure:
        {
            "hook_event_name": "SessionStart",  # PascalCase hook name
            "session_id": "abc123",             # Session identifier
            "cwd": "/path/to/project",
            "timestamp": "2025-01-15T10:30:00Z", # ISO timestamp
            # ... other hook-specific fields
        }

        Note: Gobby's ghook-managed hook command wraps this in:
        {
            "source": "<provider>",
            "hook_type": "SessionStart",
            "input_data": {...}  # The actual provider payload
        }

        Args:
            native_event: Raw payload from the ghook-managed hook command

        Returns:
            Unified HookEvent with normalized fields.
        """
        # Extract from dispatcher wrapper format (matches Claude's structure)
        hook_type = native_event.get("hook_type", "")
        input_data = native_event.get("input_data", {})

        # If input_data is empty, the native_event might BE the input_data
        # (for direct provider calls without dispatcher wrapper)
        if not input_data and (
            "hook_event_name" in native_event or "hookEventName" in native_event
        ):
            input_data = native_event
            hook_type = native_event.get("hook_event_name") or native_event.get("hookEventName", "")
        if not hook_type and input_data:
            hook_type = input_data.get("hook_event_name") or input_data.get("hookEventName", "")

        # Map provider hook type to unified event type.
        # Fall back to NOTIFICATION for unknown types (fail-open)
        event_type = self.EVENT_MAP.get(hook_type, HookEventType.NOTIFICATION)

        # Extract session_id
        session_id = input_data.get("session_id") or input_data.get("sessionId") or ""

        # Parse timestamp if present.
        timestamp_str = input_data.get("timestamp")
        if timestamp_str:
            try:
                timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                timestamp = datetime.now(UTC)
        else:
            timestamp = datetime.now(UTC)

        # Get machine_id from payload (base adapter injects if missing)
        machine_id = input_data.get("machine_id")

        # Normalize tool name if present (for tool-related hooks)
        raw_tool_name = input_data.get("tool_name") or input_data.get("toolName")
        if raw_tool_name:
            original_tool = str(raw_tool_name)
            normalized_tool = self.normalize_tool_name(original_tool)
            # Store both for logging/debugging
            metadata = {
                "original_tool_name": original_tool,
                "normalized_tool_name": normalized_tool,
            }
        else:
            metadata = {}
        metadata["_native_hook_type"] = hook_type
        self._copy_platform_session_metadata(native_event, metadata)

        # Normalize event data for CLI-agnostic processing
        # This allows downstream code to use consistent field names
        normalized_data = self._normalize_event_data(input_data)

        return HookEvent(
            event_type=event_type,
            session_id=session_id,
            source=self.source,
            timestamp=timestamp,
            machine_id=machine_id,
            cwd=input_data.get("cwd"),
            data=normalized_data,
            metadata=metadata,
        )

    def translate_from_hook_response(
        self, response: HookResponse, hook_type: str | None = None
    ) -> dict[str, Any]:
        """Convert HookResponse to the ACP-style provider response format.

        ACP-style providers expect responses in this format:
        {
            "decision": "allow" | "deny",     # "block" is mapped to "deny"
            "continue": True/False,            # False only for hard-stop denials
            "reason": "...",                   # Optional reason for decision
            "hookSpecificOutput": {            # Hook-specific response data
                "additionalContext": "...",    # Context to inject
                "llm_request": {...},          # For BeforeModel hooks
                "toolConfig": {...}            # For BeforeToolSelection hooks
            }
        }

        Exit codes: always 0 because providers treat non-zero as "hook failed".
        Recoverable tool-hook denials are conveyed via top-level
        decision/reason with continue=true so the provider skips only the tool call.
        Unknown/no-capability denials remain hard stops with continue=false.

        Args:
            response: Unified HookResponse from HookManager.
            hook_type: Original provider hook type (e.g., "SessionStart")
                      Used to format hookSpecificOutput appropriately.

        Returns:
            Dict in the provider's expected format.
        """
        capabilities = get_provider_capabilities(self.source)
        capability = capabilities.get_hook(hook_type)
        hook_event_name = self._response_hook_event_name(hook_type)
        resolved_hook_type = hook_event_name or hook_type
        is_denied = response.decision in ("deny", "block")
        recoverable_denial_hook = capability is not None and capability.event_type in {
            HookEventType.BEFORE_TOOL,
            HookEventType.AFTER_TOOL,
        }
        should_continue = not is_denied or recoverable_denial_hook
        event_logger = self._event_logger()
        record_unsupported_response_fields(
            response,
            provider=self.source,
            hook_type=hook_type,
            capability=capability,
            event_logger=event_logger,
        )
        normalized_reason = normalize_adapter_response_reason(
            response,
            adapter_name=self.__class__.__name__,
            hook_type=hook_type,
            logger=event_logger,
        )
        if is_denied and normalized_reason == ADAPTER_EMPTY_BLOCK_REASON_SENTINEL:
            should_continue = False

        result: dict[str, Any] = {
            "decision": "deny" if is_denied else response.decision,
            "continue": should_continue,
        }

        # Add reason if present
        if normalized_reason:
            result["reason"] = normalized_reason

        session_start_hook = resolved_hook_type in {"SessionStart", "session_start"}
        context_channel = (
            capability.context_channel if capability else ContextChannel.ADDITIONAL_CONTEXT
        )

        # Build hookSpecificOutput based on hook type
        hook_specific: dict[str, Any] = {}
        context_parts: list[tuple[str, str]] = []

        # Add context injection if present
        if response.context:
            context_parts.append(("response.context", response.context))

        # SessionStart startup context should be injected once via
        # additionalContext, not duplicated into systemMessage.
        if response.system_message:
            if session_start_hook:
                context_parts.insert(0, ("system_message", response.system_message))
            else:
                result["systemMessage"] = response.system_message

        # Add session/terminal context for hooks that support additionalContext
        # Parity with Claude Code: inject on SessionStart, BeforeAgent, BeforeTool, AfterTool
        hooks_with_metadata_context = {
            "SessionStart",
            "BeforeAgent",
            "BeforeTool",
            "AfterTool",
            "session_start",
            "user_prompt_submit",
            "pre_tool_use",
            "post_tool_use",
        }
        if resolved_hook_type in hooks_with_metadata_context and response.metadata:
            session_id = response.metadata.get("session_id")

            if session_id:
                context_lines = build_first_hook_session_metadata_lines(
                    response.metadata,
                    include_session_id_line=not (
                        session_start_hook
                        and system_message_has_session_banner(response.system_message)
                    ),
                )
                if context_lines:
                    context_parts.append(("metadata", "\n".join(context_lines)))

        if resolved_hook_type in hooks_with_metadata_context and context_parts and hook_event_name:
            hook_specific["hookEventName"] = hook_event_name

        # Handle BeforeModel-specific output (llm_request modification)
        if resolved_hook_type == "BeforeModel" and response.modify_args:
            hook_specific["llm_request"] = response.modify_args

        # Handle BeforeToolSelection-specific output (toolConfig modification)
        if resolved_hook_type == "BeforeToolSelection" and response.modify_args:
            hook_specific["toolConfig"] = response.modify_args

        if context_parts and context_channel is ContextChannel.ADDITIONAL_CONTEXT:
            hook_specific["additionalContext"] = truncate_context_for_adapter(
                "\n\n".join(part for _, part in context_parts),
                provider=self.source,
                hook_type=hook_type,
                destination_channel=ContextChannel.ADDITIONAL_CONTEXT,
                contributor_sizes={label: len(part) for label, part in context_parts},
                event_logger=event_logger,
                **persist_kwargs_from_hook_response(response, self._hook_manager),
            )

        # Only add hookSpecificOutput if there's content
        if hook_specific:
            result["hookSpecificOutput"] = hook_specific

        return result

    def handle_native(
        self, native_event: dict[str, Any], hook_manager: "HookManager"
    ) -> dict[str, Any]:
        """Main entry point for HTTP endpoint.

        Translates a native ACP-style event, processes through HookManager,
        and returns the provider response format.

        Args:
            native_event: Raw payload from a ghook-managed hook command
            hook_manager: HookManager instance for processing.

        Returns:
            Response dict in the provider's expected format.
        """
        # Translate to unified HookEvent
        hook_event = self.translate_to_hook_event(native_event)

        # Get original hook type for response formatting
        hook_type = native_event.get("hook_type", "")
        if not hook_type:
            hook_type = native_event.get("hook_event_name", "")
        if not hook_type:
            hook_type = native_event.get("hookEventName", "")
        if not hook_type:
            hook_type = native_event.get("input_data", {}).get("hook_event_name", "")
        if not hook_type:
            hook_type = native_event.get("input_data", {}).get("hookEventName", "")
        input_data = native_event.get("input_data", {}) or {}
        if not input_data and (
            "hook_event_name" in native_event or "hookEventName" in native_event
        ):
            input_data = native_event

        # Process through HookManager
        self._hook_manager = hook_manager
        hook_response = hook_manager.handle(hook_event)

        # Translate response back to the provider format.
        result = self.translate_from_hook_response(hook_response, hook_type=hook_type)

        # Normal AfterAgent blocks should retry so stop gates keep the agent
        # alive. But when the user interrupts the turn, providers may still fire
        # AfterAgent without a completed prompt_response; in that case, kill the
        # loop so the cancel doesn't get trapped in a retry cycle.
        if hook_type == "AfterAgent" and hook_response.decision == "block":
            result["continue"] = not self._is_cancelled_after_agent(input_data)

        return result


__all__ = ["ACPHookAdapter"]

"""Copilot adapter for hook translation.

This adapter translates between GitHub Copilot CLI's hook format and the unified
HookEvent/HookResponse models.

Copilot Hook Types (similar to Claude Code but with camelCase):
- sessionStart, sessionEnd: Session lifecycle
- userPromptSubmitted: Before user prompt validation
- preToolUse, postToolUse: Tool execution lifecycle
- errorOccurred: Error notifications

Key differences from Claude Code:
- Uses camelCase hook names (preToolUse vs pre-tool-use)
- Uses `toolName` instead of `tool_name`
- Uses `toolArgs` instead of `tool_input`
- Uses `toolResult.textResultForLlm` for tool output
- Response uses `permissionDecision` (allow/deny) instead of continue/decision
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.adapters.base import BaseAdapter
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.llm.sdk_utils import truncate_additional_context

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager


class CopilotAdapter(BaseAdapter):
    """Adapter for GitHub Copilot CLI hook translation.

    This adapter:
    1. Translates Copilot's camelCase hook payloads to unified HookEvent
    2. Translates HookResponse back to Copilot's expected format
    3. Normalizes tool names and arguments to standard format
    """

    source = SessionSource.COPILOT

    # Event type mapping: Copilot hook names -> unified HookEventType
    # Copilot uses camelCase hook names in the payload's "hook_type" field
    EVENT_MAP: dict[str, HookEventType] = {
        "sessionStart": HookEventType.SESSION_START,
        "sessionEnd": HookEventType.SESSION_END,
        "userPromptSubmitted": HookEventType.BEFORE_AGENT,
        "preToolUse": HookEventType.BEFORE_TOOL,
        "postToolUse": HookEventType.AFTER_TOOL,
        "errorOccurred": HookEventType.NOTIFICATION,
        "stop": HookEventType.STOP,
        "preCompact": HookEventType.PRE_COMPACT,
        "notification": HookEventType.NOTIFICATION,
    }

    # Map Copilot hook types to PascalCase event names for response
    # Uses incoming camelCase hook_type (e.g., "preToolUse" -> "PreToolUse")
    HOOK_EVENT_NAME_MAP: dict[str, str] = {
        "sessionStart": "SessionStart",
        "sessionEnd": "SessionEnd",
        "userPromptSubmitted": "UserPromptSubmitted",
        "stop": "Stop",
        "preToolUse": "PreToolUse",
        "postToolUse": "PostToolUse",
        "preCompact": "PreCompact",
        "notification": "Notification",
        "errorOccurred": "Notification",
    }

    def __init__(self, hook_manager: "HookManager | None" = None):
        """Initialize the Copilot adapter.

        Args:
            hook_manager: Reference to HookManager for handling events.
                         If None, the adapter can only translate (not handle events).
        """
        self._hook_manager = hook_manager

    def _normalize_event_data(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize Copilot event data for CLI-agnostic processing.

        Handles Copilot-specific nested ``toolResult`` extraction, then
        delegates to the shared ``normalize_tool_fields`` for camelCase
        aliases (``toolName`` → ``tool_name``, ``toolArgs`` → ``tool_input``
        with JSON-string parsing) and MCP enrichment.

        Args:
            input_data: Raw input data from Copilot CLI

        Returns:
            Enriched data dict with normalized fields added
        """
        from gobby.hooks.normalization import normalize_tool_fields

        data = dict(input_data)

        # Copilot-specific: extract toolResult.textResultForLlm → tool_output
        tool_result = data.get("toolResult", {})
        if tool_result and "tool_output" not in data:
            if isinstance(tool_result, dict):
                text_result = tool_result.get("textResultForLlm")
                if text_result:
                    data["tool_output"] = text_result
                result_type = tool_result.get("resultType")
                if result_type == "error":
                    data["is_error"] = True
            else:
                data["tool_output"] = tool_result

        # Shared field alias + MCP normalization
        # (handles toolName→tool_name, toolArgs→tool_input with JSON parsing,
        #  mcp__ prefix extraction, tool_result→tool_output)
        normalize_tool_fields(data)

        return data

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent:
        """Convert Copilot native event to unified HookEvent.

        Copilot payloads have the structure:
        {
            "hook_type": "preToolUse",  # camelCase hook name
            "input_data": {
                "session_id": "abc123",
                "cwd": "/path/to/project",
                "toolName": "Read",
                "toolArgs": {"path": "/file.py"},
                # For post-tool:
                "toolResult": {
                    "resultType": "success",
                    "textResultForLlm": "file contents..."
                }
            }
        }

        Args:
            native_event: Raw payload from Copilot hook dispatcher

        Returns:
            Unified HookEvent with normalized fields.
        """
        hook_type = native_event.get("hook_type", "")
        input_data = native_event.get("input_data", {})

        # Map Copilot hook type to unified event type
        # Fall back to NOTIFICATION for unknown types (fail-open)
        event_type = self.EVENT_MAP.get(hook_type, HookEventType.NOTIFICATION)

        # Extract session_id
        session_id = input_data.get("session_id", "")

        # Check for error in tool result
        tool_result = input_data.get("toolResult", {})
        is_error = False
        if isinstance(tool_result, dict):
            is_error = tool_result.get("resultType") == "error"

        metadata = {"is_failure": is_error} if is_error else {}

        # Normalize event data for CLI-agnostic processing
        normalized_data = self._normalize_event_data(input_data)

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
        """Convert HookResponse to Copilot's expected format.

        Copilot expects responses in this format:
        {
            "permissionDecision": "allow" | "deny",
            "permissionDecisionReason": "...",  # Optional reason
            "hookSpecificOutput": {
                "additionalContext": "..."  # Context to inject
            }
        }

        Args:
            response: Unified HookResponse from HookManager.
            hook_type: Original Copilot hook type (e.g., "preToolUse")
                      Used to format hookSpecificOutput appropriately.

        Returns:
            Dict in Copilot's expected format.
        """
        # Map decision to Copilot's permissionDecision format
        # Copilot uses "allow"/"deny" directly
        if response.decision in ("deny", "block"):
            permission_decision = "deny"
        else:
            permission_decision = "allow"

        result: dict[str, Any] = {
            "permissionDecision": permission_decision,
        }

        # Add reason if present
        if response.reason:
            result["permissionDecisionReason"] = response.reason

        # Add system message if present
        if response.system_message:
            result["systemMessage"] = response.system_message

        # Build hookSpecificOutput with additionalContext for model context injection
        hook_event_name = self.HOOK_EVENT_NAME_MAP.get(hook_type or "", "Unknown")
        additional_context_parts: list[str] = []

        # Add workflow-injected context
        if response.context:
            additional_context_parts.append(response.context)

        # Add session identifiers from metadata
        if response.metadata:
            gobby_session_id = response.metadata.get("session_id")
            session_ref = response.metadata.get("session_ref")
            external_id = response.metadata.get("external_id")
            is_first_hook = response.metadata.get("_first_hook_for_session", False)

            if gobby_session_id:
                if is_first_hook:
                    # First hook: inject full metadata
                    context_lines = []
                    if session_ref:
                        context_lines.append(
                            f"Gobby Session ID: {session_ref} (or {gobby_session_id})"
                        )
                    else:
                        context_lines.append(f"Gobby Session ID: {gobby_session_id}")
                    if external_id:
                        context_lines.append(
                            f"CLI-Specific Session ID (external_id): {external_id}"
                        )
                    if response.metadata.get("parent_session_id"):
                        context_lines.append(
                            f"parent_session_id: {response.metadata['parent_session_id']}"
                        )
                    if response.metadata.get("machine_id"):
                        context_lines.append(f"machine_id: {response.metadata['machine_id']}")
                    if response.metadata.get("project_id"):
                        context_lines.append(f"project_id: {response.metadata['project_id']}")
                    # Add terminal context
                    if response.metadata.get("terminal_term_program"):
                        context_lines.append(
                            f"terminal: {response.metadata['terminal_term_program']}"
                        )
                    if response.metadata.get("terminal_parent_pid"):
                        context_lines.append(
                            f"parent_pid: {response.metadata['terminal_parent_pid']}"
                        )
                    additional_context_parts.append("\n".join(context_lines))
                else:
                    # Subsequent hooks: inject minimal session ref only
                    if session_ref:
                        additional_context_parts.append(f"Gobby Session ID: {session_ref}")

        # Build hookSpecificOutput if we have any context to inject
        valid_hook_event_names = {
            "PreToolUse",
            "UserPromptSubmitted",
            "PostToolUse",
            "SessionStart",
        }
        if additional_context_parts and hook_event_name in valid_hook_event_names:
            result["hookSpecificOutput"] = {
                "hookEventName": hook_event_name,
                "additionalContext": truncate_additional_context(
                    "\n\n".join(additional_context_parts)
                ),
            }

        return result

    def handle_native(
        self, native_event: dict[str, Any], hook_manager: "HookManager"
    ) -> dict[str, Any]:
        """Main entry point for HTTP endpoint.

        Translates native Copilot event, processes through HookManager,
        and returns response in Copilot's expected format.

        Args:
            native_event: Raw payload from Copilot hook dispatcher
            hook_manager: HookManager instance for processing.

        Returns:
            Response dict in Copilot's expected format.
        """
        # Translate to unified HookEvent
        hook_event = self.translate_to_hook_event(native_event)

        # Get original hook type for response formatting
        hook_type = native_event.get("hook_type", "")

        # Process through HookManager
        hook_response = hook_manager.handle(hook_event)

        # Translate response back to Copilot format
        return self.translate_from_hook_response(hook_response, hook_type=hook_type)

"""Claude Code adapter for hook translation.

This adapter translates between Claude Code's native hook format and the unified
HookEvent/HookResponse models. It implements the strangler fig pattern for safe
migration from the existing HookManager.execute() method.

Claude Code Hook Types (12 total):
- session-start, session-end: Session lifecycle
- user-prompt-submit: Before user prompt validation
- pre-tool-use, post-tool-use, post-tool-use-failure: Tool lifecycle
- pre-compact: Context compaction
- stop: Agent stops
- subagent-start, subagent-stop: Subagent lifecycle
- permission-request: Permission requests (future)
- notification: System notifications
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.adapters.base import (
    BaseAdapter,
    build_first_hook_session_metadata_lines,
    system_message_has_session_banner,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.llm.sdk_utils import compress_and_truncate

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager


class ClaudeCodeAdapter(BaseAdapter):
    """Adapter for Claude Code CLI hook translation.

    This adapter:
    1. Translates Claude Code's kebab-case hook payloads to unified HookEvent
    2. Translates HookResponse back to Claude Code's expected format
    3. Calls HookManager.handle() with unified HookEvent model

    Phase 2C Migration Complete:
    - Now using HookManager.handle(HookEvent) for all hooks
    - Legacy execute() path available via set_legacy_mode(True) for rollback
    """

    source = SessionSource.CLAUDE

    # Event type mapping: Claude Code hook names -> unified HookEventType
    # Claude Code uses kebab-case hook names in the payload's "hook_type" field
    EVENT_MAP: dict[str, HookEventType] = {
        "session-start": HookEventType.SESSION_START,
        "session-end": HookEventType.SESSION_END,
        "user-prompt-submit": HookEventType.BEFORE_AGENT,
        "stop": HookEventType.STOP,
        "pre-tool-use": HookEventType.BEFORE_TOOL,
        "post-tool-use": HookEventType.AFTER_TOOL,
        "post-tool-use-failure": HookEventType.AFTER_TOOL,  # Same as AFTER_TOOL with error flag
        "pre-compact": HookEventType.PRE_COMPACT,
        "subagent-start": HookEventType.SUBAGENT_START,
        "subagent-stop": HookEventType.SUBAGENT_STOP,
        "permission-request": HookEventType.PERMISSION_REQUEST,
        "notification": HookEventType.NOTIFICATION,
    }

    def __init__(self, hook_manager: "HookManager | None" = None):
        """Initialize the Claude Code adapter.

        Args:
            hook_manager: Reference to HookManager for delegation.
                         If None, the adapter can only translate (not handle events).
        """
        self._hook_manager = hook_manager

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent:
        """Convert Claude Code native event to unified HookEvent.

        Claude Code payloads have the structure:
        {
            "hook_type": "session-start",  # kebab-case hook name
            "input_data": {
                "session_id": "abc123",    # Claude calls this session_id but it's external_id
                "machine_id": "...",
                "cwd": "/path/to/project",
                "transcript_path": "...",
                # ... other hook-specific fields
            }
        }

        Args:
            native_event: Raw payload from Claude Code's ghook-managed hook command

        Returns:
            Unified HookEvent with normalized fields.
        """
        hook_type = native_event.get("hook_type", "")
        input_data = native_event.get("input_data") or {}

        # Map Claude hook type to unified event type
        # Fall back to NOTIFICATION for unknown types (fail-open)
        event_type = self.EVENT_MAP.get(hook_type, HookEventType.NOTIFICATION)

        # Extract session_id (Claude calls it session_id but it's the external_id)
        session_id = input_data.get("session_id", "")

        # Normalize event data for CLI-agnostic processing FIRST
        # so that is_error detection (Phase 3) runs before we build metadata
        normalized_data = self._normalize_event_data(input_data)

        # Check for failure: explicit hook type OR inferred from tool output
        is_failure = hook_type == "post-tool-use-failure" or normalized_data.get("is_error", False)
        metadata = {"is_failure": is_failure} if is_failure else {}

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

    def _normalize_event_data(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize Claude Code event data for CLI-agnostic processing.

        Delegates to the shared ``normalize_mcp_fields`` so the same logic
        is used by both the CLI adapter and the web-chat path.

        Args:
            input_data: Raw input data from Claude Code

        Returns:
            Enriched data dict with normalized fields added
        """
        from gobby.hooks.normalization import normalize_tool_fields

        # Copy to avoid mutating the original (shared function mutates in place)
        return normalize_tool_fields(dict(input_data))

    # Map Claude Code hook types to hookEventName for hookSpecificOutput
    HOOK_EVENT_NAME_MAP: dict[str, str] = {
        "session-start": "SessionStart",
        "session-end": "SessionEnd",
        "user-prompt-submit": "UserPromptSubmit",
        "stop": "Stop",
        "pre-tool-use": "PreToolUse",
        "post-tool-use": "PostToolUse",
        "post-tool-use-failure": "PostToolUse",
        "pre-compact": "PreCompact",
        "subagent-start": "SubagentStart",
        "subagent-stop": "SubagentStop",
        "permission-request": "PermissionRequest",
        "notification": "Notification",
    }

    def translate_from_hook_response(
        self, response: HookResponse, hook_type: str | None = None
    ) -> dict[str, Any]:
        """Convert HookResponse to Claude Code's expected format.

        Claude Code expects responses in this format:
        {
            "continue": True/False,        # Whether to continue execution
            "stopReason": "...",           # Reason if stopped (optional)
            "decision": "approve"/"block", # Tool decision
            "hookSpecificOutput": {        # Hook-specific data
                "hookEventName": "SessionStart",  # Required!
                "additionalContext": "..."  # Context to inject into Claude
            }
        }

        Args:
            response: Unified HookResponse from HookManager.
            hook_type: Original Claude Code hook type (e.g., "session-start")
                      Used to set hookEventName in hookSpecificOutput.

        Returns:
            Dict in Claude Code's expected format.
        """
        # Map decision to continue flag
        # Both "deny" and "block" should stop execution
        should_continue = response.decision not in ("deny", "block")

        result: dict[str, Any] = {
            "continue": should_continue,
        }

        # Add stop reason if denied or blocked
        if response.decision in ("deny", "block") and response.reason:
            result["stopReason"] = response.reason

        # Add tool decision for pre-tool-use hooks
        # Claude Code schema: decision uses "approve"/"block"
        # permissionDecision uses "allow"/"deny"/"ask"
        if response.decision in ("deny", "block"):
            result["decision"] = "block"
        else:
            result["decision"] = "approve"

        # Add hookSpecificOutput with additionalContext for model context injection
        # This includes both workflow inject_context AND session identifiers
        hook_event_name = self.HOOK_EVENT_NAME_MAP.get(hook_type or "", "Unknown")
        session_start_hook = hook_event_name == "SessionStart"
        additional_context_parts: list[str] = []

        # SessionStart startup context should be injected once through
        # additionalContext, not duplicated into systemMessage.
        if response.system_message:
            if session_start_hook:
                additional_context_parts.append(response.system_message)
            else:
                result["systemMessage"] = response.system_message

        # Add workflow-injected context (from inject_context action)
        # This is the primary way to inject context visible to the model
        if response.context:
            additional_context_parts.append(response.context)

        # Add session identifiers from metadata
        # Note: "session_id" in metadata is Gobby's internal platform session ID
        #       "external_id" in metadata is the CLI's session UUID
        #       "session_ref" is the short #N format for easier reference
        # Token optimization: Only inject full metadata on first hook per session
        if response.metadata:
            gobby_session_id = response.metadata.get("session_id")
            if gobby_session_id:
                context_lines = build_first_hook_session_metadata_lines(
                    response.metadata,
                    include_session_id_line=not (
                        session_start_hook
                        and system_message_has_session_banner(response.system_message)
                    ),
                )
                if context_lines:
                    additional_context_parts.append("\n".join(context_lines))

        # Build hookSpecificOutput if we have any context to inject
        # Only include hookSpecificOutput for hook types that Claude Code's schema accepts
        # Valid hookEventName values: PreToolUse, UserPromptSubmit, PostToolUse, SessionStart
        valid_hook_event_names = {"PreToolUse", "UserPromptSubmit", "PostToolUse", "SessionStart"}
        if additional_context_parts and hook_event_name in valid_hook_event_names:
            result["hookSpecificOutput"] = {
                "hookEventName": hook_event_name,
                "additionalContext": compress_and_truncate("\n\n".join(additional_context_parts))[
                    0
                ],
            }

        # PreToolUse: rewrite tool input via updatedInput
        if response.modified_input and hook_event_name == "PreToolUse":
            hook_output = result.setdefault("hookSpecificOutput", {"hookEventName": "PreToolUse"})
            hook_output["updatedInput"] = response.modified_input
            if response.auto_approve:
                hook_output["permissionDecision"] = "allow"

        return result

    def handle_native(
        self, native_event: dict[str, Any], hook_manager: "HookManager"
    ) -> dict[str, Any]:
        """Main entry point for HTTP endpoint.

        Args:
            native_event: Raw payload from Claude Code's ghook-managed hook command
            hook_manager: HookManager instance for processing.

        Returns:
            Response dict in Claude Code's expected format.
        """
        # Translate to HookEvent
        hook_event = self.translate_to_hook_event(native_event)

        # Use HookEvent-based handler
        hook_type = native_event.get("hook_type", "")
        hook_response = hook_manager.handle(hook_event)
        return self.translate_from_hook_response(hook_response, hook_type=hook_type)

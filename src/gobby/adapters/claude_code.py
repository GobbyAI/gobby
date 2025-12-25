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

from datetime import datetime
from typing import TYPE_CHECKING, Any

from gobby.adapters.base import BaseAdapter
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

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
        "stop": HookEventType.AFTER_AGENT,
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
            hook_manager: Reference to HookManager for strangler fig delegation.
                         If None, the adapter can only translate (not handle events).
        """
        self._hook_manager = hook_manager
        # Phase 2C: Use new handle() path with unified HookEvent model
        # Note: systemMessage handoff notification bug exists in both paths (see plan-multi-cli.md)
        self._use_legacy = False

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
            native_event: Raw payload from Claude Code's hook_dispatcher.py

        Returns:
            Unified HookEvent with normalized fields.
        """
        hook_type = native_event.get("hook_type", "")
        input_data = native_event.get("input_data", {})

        # Map Claude hook type to unified event type
        # Fall back to NOTIFICATION for unknown types (fail-open)
        event_type = self.EVENT_MAP.get(hook_type, HookEventType.NOTIFICATION)

        # Extract session_id (Claude calls it session_id but it's the external_id)
        session_id = input_data.get("session_id", "")

        # Check for failure flag in post-tool-use-failure
        is_failure = hook_type == "post-tool-use-failure"
        metadata = {"is_failure": is_failure} if is_failure else {}

        return HookEvent(
            event_type=event_type,
            session_id=session_id,
            source=self.source,
            timestamp=datetime.now(),
            machine_id=input_data.get("machine_id"),
            cwd=input_data.get("cwd"),
            data=input_data,
            metadata=metadata,
        )

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
        should_continue = response.decision != "deny"

        result: dict[str, Any] = {
            "continue": should_continue,
        }

        # Add stop reason if denied
        if response.decision == "deny" and response.reason:
            result["stopReason"] = response.reason

        # Add context injection if present (inside hookSpecificOutput per Claude Code schema)
        # hookEventName is REQUIRED by Claude Code's schema
        if response.context:
            hook_event_name = self.HOOK_EVENT_NAME_MAP.get(hook_type or "", "Unknown")
            result["hookSpecificOutput"] = {
                "hookEventName": hook_event_name,
                "additionalContext": response.context,
            }

        # Add user-visible system message if present (e.g., handoff notification)
        if response.system_message:
            result["systemMessage"] = response.system_message

        # Add tool decision for pre-tool-use hooks
        # Claude Code schema: decision uses "approve"/"block"
        # permissionDecision uses "allow"/"deny"/"ask"
        if response.decision == "deny":
            result["decision"] = "block"
        else:
            result["decision"] = "approve"

        # Note: metadata is NOT passed to Claude Code - it's not in their schema
        # Metadata is for internal use only (e.g., logging, debugging)

        return result

    def handle_native(
        self, native_event: dict[str, Any], hook_manager: "HookManager"
    ) -> dict[str, Any]:
        """Main entry point for HTTP endpoint.

        Strangler fig pattern:
        - Phase 2A-2B: Delegates to existing execute() — validates translation only
        - Phase 2C+: Calls new handle() with HookEvent

        Note: This method is synchronous for Phase 2A-2B compatibility with
        the existing execute() method. In Phase 2C+, it will become async
        when handle() is implemented as async.

        Args:
            native_event: Raw payload from Claude Code's hook_dispatcher.py
            hook_manager: HookManager instance for processing.

        Returns:
            Response dict in Claude Code's expected format.
        """
        # Always translate (validates our mapping is correct)
        hook_event = self.translate_to_hook_event(native_event)

        # Phase 2C+: Use new HookEvent-based handler
        # Legacy execute() path removed as HookManager.execute is deprecated/removed.
        hook_type = native_event.get("hook_type", "")
        hook_response = hook_manager.handle(hook_event)
        return self.translate_from_hook_response(hook_response, hook_type=hook_type)

    def set_legacy_mode(self, use_legacy: bool) -> None:
        """Toggle between legacy and new code paths.

        This method is used during the strangler fig migration to switch
        between delegating to execute() and calling handle() directly.

        Args:
            use_legacy: If True, use legacy execute() path. If False, use new handle() path.
        """
        self._use_legacy = use_legacy

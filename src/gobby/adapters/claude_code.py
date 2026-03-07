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

from gobby.adapters.base import BaseAdapter
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.llm.sdk_utils import truncate_additional_context

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
            native_event: Raw payload from Claude Code's hook_dispatcher.py

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

        # Check for failure flag in post-tool-use-failure
        is_failure = hook_type == "post-tool-use-failure"
        metadata = {"is_failure": is_failure} if is_failure else {}

        # Normalize event data for CLI-agnostic processing
        # This allows downstream code to use consistent field names
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

        # Add system_message to systemMessage (for system-level messages)
        # Note: response.context goes to additionalContext below (visible to model)
        if response.system_message:
            result["systemMessage"] = response.system_message

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
        additional_context_parts: list[str] = []

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
            session_ref = response.metadata.get("session_ref")
            external_id = response.metadata.get("external_id")
            is_first_hook = response.metadata.get("_first_hook_for_session", False)

            if gobby_session_id:
                if is_first_hook:
                    # First hook: inject full metadata (~60-100 tokens)
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
                    # Add terminal context (non-null values only)
                    if response.metadata.get("terminal_term_program"):
                        context_lines.append(
                            f"terminal: {response.metadata['terminal_term_program']}"
                        )
                    if response.metadata.get("terminal_tty"):
                        context_lines.append(f"tty: {response.metadata['terminal_tty']}")
                    if response.metadata.get("terminal_parent_pid"):
                        context_lines.append(
                            f"parent_pid: {response.metadata['terminal_parent_pid']}"
                        )
                    # Add terminal-specific session IDs (only one will be present)
                    for key in [
                        "terminal_iterm_session_id",
                        "terminal_term_session_id",
                        "terminal_kitty_window_id",
                        "terminal_tmux_pane",
                        "terminal_vscode_terminal_id",
                        "terminal_alacritty_socket",
                    ]:
                        if response.metadata.get(key):
                            # Use friendlier names in output
                            friendly_name = key.replace("terminal_", "").replace("_", " ")
                            context_lines.append(f"{friendly_name}: {response.metadata[key]}")
                    additional_context_parts.append("\n".join(context_lines))
                else:
                    # Subsequent hooks: inject minimal session ref only (~8 tokens)
                    if session_ref:
                        additional_context_parts.append(f"Gobby Session ID: {session_ref}")

        # Build hookSpecificOutput if we have any context to inject
        # Only include hookSpecificOutput for hook types that Claude Code's schema accepts
        # Valid hookEventName values: PreToolUse, UserPromptSubmit, PostToolUse, SessionStart
        valid_hook_event_names = {"PreToolUse", "UserPromptSubmit", "PostToolUse", "SessionStart"}
        if additional_context_parts and hook_event_name in valid_hook_event_names:
            result["hookSpecificOutput"] = {
                "hookEventName": hook_event_name,
                "additionalContext": truncate_additional_context(
                    "\n\n".join(additional_context_parts)
                ),
            }

        # PreToolUse: rewrite tool input via updatedInput
        if response.modified_input and hook_event_name == "PreToolUse":
            hook_output = result.setdefault("hookSpecificOutput", {"hookEventName": "PreToolUse"})
            hook_output["updatedInput"] = response.modified_input
            if response.auto_approve:
                hook_output["permissionDecision"] = "allow"

        # PostToolUse: replace MCP tool output via updatedMCPToolOutput
        if response.modified_output is not None and hook_event_name == "PostToolUse":
            hook_output = result.setdefault("hookSpecificOutput", {"hookEventName": "PostToolUse"})
            hook_output["updatedMCPToolOutput"] = response.modified_output

        return result

    def handle_native(
        self, native_event: dict[str, Any], hook_manager: "HookManager"
    ) -> dict[str, Any]:
        """Main entry point for HTTP endpoint.

        Args:
            native_event: Raw payload from Claude Code's hook_dispatcher.py
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

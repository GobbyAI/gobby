"""Gemini CLI adapter for hook translation.

This adapter translates between Gemini CLI's native hook format and the unified
HookEvent/HookResponse models.

Gemini CLI Hook Types (11 total):
- SessionStart, SessionEnd: Session lifecycle
- BeforeAgent, AfterAgent: Agent turn lifecycle
- BeforeTool, AfterTool: Tool execution lifecycle
- BeforeToolSelection: Before tool selection (Gemini-only)
- BeforeModel, AfterModel: Model call lifecycle (Gemini-only)
- PreCompress: Context compression (maps to PRE_COMPACT)
- Notification: System notifications

Key differences from Claude Code:
- Uses PascalCase hook names (SessionStart vs session-start)
- Uses `hook_event_name` field instead of `hook_type`
- Has BeforeToolSelection, BeforeModel, AfterModel (not in Claude)
- Missing PermissionRequest, SubagentStart, SubagentStop (Claude-only)
- Different tool names (RunShellCommand vs Bash)
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.adapters.base import BaseAdapter
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.llm.sdk_utils import truncate_additional_context

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager


class GeminiAdapter(BaseAdapter):
    """Adapter for Gemini CLI hook translation.

    This adapter:
    1. Translates Gemini CLI's PascalCase hook payloads to unified HookEvent
    2. Translates HookResponse back to Gemini CLI's expected format
    3. Calls HookManager.handle() with unified HookEvent model
    """

    source = SessionSource.GEMINI

    # Event type mapping: Gemini CLI hook names -> unified HookEventType
    # Gemini CLI uses PascalCase hook names in the payload's "hook_event_name" field
    EVENT_MAP: dict[str, HookEventType] = {
        "SessionStart": HookEventType.SESSION_START,
        "SessionEnd": HookEventType.SESSION_END,
        "BeforeAgent": HookEventType.BEFORE_AGENT,
        "AfterAgent": HookEventType.AFTER_AGENT,
        "BeforeTool": HookEventType.BEFORE_TOOL,
        "AfterTool": HookEventType.AFTER_TOOL,
        "BeforeToolSelection": HookEventType.BEFORE_TOOL_SELECTION,  # Gemini-only
        "BeforeModel": HookEventType.BEFORE_MODEL,  # Gemini-only
        "AfterModel": HookEventType.AFTER_MODEL,  # Gemini-only
        "PreCompress": HookEventType.PRE_COMPACT,  # Gemini calls it PreCompress
        "Notification": HookEventType.NOTIFICATION,
    }

    # Reverse mapping for response translation
    HOOK_EVENT_NAME_MAP: dict[str, str] = {
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

    # Tool name mapping: Gemini tool names -> normalized names
    # Gemini uses different tool names than Claude Code
    # This enables workflows to use Claude Code naming conventions
    TOOL_MAP: dict[str, str] = {
        # Shell/Bash
        "run_shell_command": "Bash",
        "RunShellCommand": "Bash",
        "ShellTool": "Bash",
        # File read
        "read_file": "Read",
        "ReadFile": "Read",
        "ReadFileTool": "Read",
        # File write
        "write_file": "Write",
        "WriteFile": "Write",
        "WriteFileTool": "Write",
        # File edit
        "edit_file": "Edit",
        "EditFile": "Edit",
        "EditFileTool": "Edit",
        # Search/Glob/Grep
        "GlobTool": "Glob",
        "GrepTool": "Grep",
        "search_file_content": "Grep",
        "SearchText": "Grep",
        # MCP tools (Gobby MCP server)
        "call_tool": "mcp__gobby__call_tool",
        "list_mcp_servers": "mcp__gobby__list_mcp_servers",
        "list_tools": "mcp__gobby__list_tools",
        "get_tool_schema": "mcp__gobby__get_tool_schema",
        "search_tools": "mcp__gobby__search_tools",
        "recommend_tools": "mcp__gobby__recommend_tools",
        # Skill and agent tools
        "activate_skill": "Skill",
        "delegate_to_agent": "Task",
    }

    def __init__(self, hook_manager: "HookManager | None" = None):
        """Initialize the Gemini CLI adapter.

        Args:
            hook_manager: Reference to HookManager for handling events.
                         If None, the adapter can only translate (not handle events).
        """
        self._hook_manager = hook_manager

    def normalize_tool_name(self, gemini_tool_name: str) -> str:
        """Normalize Gemini tool name to standard format.

        Args:
            gemini_tool_name: Tool name from Gemini CLI.

        Returns:
            Normalized tool name (e.g., "Bash", "Read", "Write").
        """
        return self.TOOL_MAP.get(gemini_tool_name, gemini_tool_name)

    def _normalize_event_data(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize Gemini event data for CLI-agnostic processing.

        Delegates field-alias and MCP normalization to the shared
        ``normalize_tool_fields`` helper, then applies the Gemini-specific
        TOOL_MAP (e.g. ``write_file`` → ``Write``).

        Args:
            input_data: Raw input data from Gemini CLI

        Returns:
            Enriched data dict with normalized fields added
        """
        from gobby.hooks.normalization import normalize_tool_fields

        data = dict(input_data)
        normalize_tool_fields(data)

        # Gemini-specific: map tool names to Claude Code conventions
        if "tool_name" in data:
            data["tool_name"] = self.normalize_tool_name(data["tool_name"])

        return data

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent:
        """Convert Gemini CLI native event to unified HookEvent.

        Gemini CLI payloads have the structure:
        {
            "hook_event_name": "SessionStart",  # PascalCase hook name
            "session_id": "abc123",             # Session identifier
            "cwd": "/path/to/project",
            "timestamp": "2025-01-15T10:30:00Z", # ISO timestamp
            # ... other hook-specific fields
        }

        Note: The hook_dispatcher.py wraps this in:
        {
            "source": "gemini",
            "hook_type": "SessionStart",
            "input_data": {...}  # The actual Gemini payload
        }

        Args:
            native_event: Raw payload from Gemini CLI's hook_dispatcher.py

        Returns:
            Unified HookEvent with normalized fields.
        """
        # Extract from dispatcher wrapper format (matches Claude's structure)
        hook_type = native_event.get("hook_type", "")
        input_data = native_event.get("input_data", {})

        # If input_data is empty, the native_event might BE the input_data
        # (for direct Gemini calls without dispatcher wrapper)
        if not input_data and "hook_event_name" in native_event:
            input_data = native_event
            hook_type = native_event.get("hook_event_name", "")

        # Map Gemini hook type to unified event type
        # Fall back to NOTIFICATION for unknown types (fail-open)
        event_type = self.EVENT_MAP.get(hook_type, HookEventType.NOTIFICATION)

        # Extract session_id
        session_id = input_data.get("session_id", "")

        # Parse timestamp if present (Gemini uses ISO format)
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
        if "tool_name" in input_data:
            original_tool = input_data.get("tool_name", "")
            normalized_tool = self.normalize_tool_name(original_tool)
            # Store both for logging/debugging
            metadata = {
                "original_tool_name": original_tool,
                "normalized_tool_name": normalized_tool,
            }
        else:
            metadata = {}

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
        """Convert HookResponse to Gemini CLI's expected format.

        Gemini CLI expects responses in this format:
        {
            "decision": "allow" | "deny",     # Whether to allow the action
            "reason": "...",                   # Optional reason for decision
            "hookSpecificOutput": {            # Hook-specific response data
                "additionalContext": "...",    # Context to inject
                "llm_request": {...},          # For BeforeModel hooks
                "toolConfig": {...}            # For BeforeToolSelection hooks
            }
        }

        Exit codes: 0 = allow, 2 = deny (handled by dispatcher)

        Args:
            response: Unified HookResponse from HookManager.
            hook_type: Original Gemini CLI hook type (e.g., "SessionStart")
                      Used to format hookSpecificOutput appropriately.

        Returns:
            Dict in Gemini CLI's expected format.
        """
        result: dict[str, Any] = {
            "decision": response.decision,
        }

        # Add reason if present
        if response.reason:
            result["reason"] = response.reason

        # Build hookSpecificOutput based on hook type
        hook_specific: dict[str, Any] = {}

        # Add context injection if present
        if response.context:
            hook_specific["additionalContext"] = response.context

        # Add session/terminal context for hooks that support additionalContext
        # Parity with Claude Code: inject on SessionStart, BeforeAgent, BeforeTool, AfterTool
        hooks_with_context = {"SessionStart", "BeforeAgent", "BeforeTool", "AfterTool"}
        if hook_type in hooks_with_context and response.metadata:
            session_id = response.metadata.get("session_id")
            session_ref = response.metadata.get("session_ref")
            external_id = response.metadata.get("external_id")
            is_first_hook = response.metadata.get("_first_hook_for_session", False)

            if session_id:
                hook_event_name = self.HOOK_EVENT_NAME_MAP.get(hook_type, "Unknown")

                if is_first_hook:
                    # First hook: inject full metadata (~60-100 tokens)
                    context_lines = []
                    if session_ref:
                        context_lines.append(f"Gobby Session ID: {session_ref} (or {session_id})")
                    else:
                        context_lines.append(f"Gobby Session ID: {session_id}")
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
                    # Add terminal-specific session IDs
                    for key in [
                        "terminal_iterm_session_id",
                        "terminal_term_session_id",
                        "terminal_kitty_window_id",
                        "terminal_tmux_pane",
                        "terminal_vscode_terminal_id",
                        "terminal_alacritty_socket",
                    ]:
                        if response.metadata.get(key):
                            friendly_name = key.replace("terminal_", "").replace("_", " ")
                            context_lines.append(f"{friendly_name}: {response.metadata[key]}")

                    hook_specific["hookEventName"] = hook_event_name
                    # Append to existing additionalContext if present
                    existing = hook_specific.get("additionalContext", "")
                    new_context = "\n".join(context_lines)
                    hook_specific["additionalContext"] = (
                        f"{existing}\n{new_context}" if existing else new_context
                    )
                else:
                    # Subsequent hooks: inject minimal session ref only (~8 tokens)
                    if session_ref:
                        hook_specific["hookEventName"] = hook_event_name
                        existing = hook_specific.get("additionalContext", "")
                        minimal_context = f"Gobby Session ID: {session_ref}"
                        hook_specific["additionalContext"] = (
                            f"{existing}\n{minimal_context}" if existing else minimal_context
                        )

        # Handle BeforeModel-specific output (llm_request modification)
        if hook_type == "BeforeModel" and response.modify_args:
            hook_specific["llm_request"] = response.modify_args

        # Handle BeforeToolSelection-specific output (toolConfig modification)
        if hook_type == "BeforeToolSelection" and response.modify_args:
            hook_specific["toolConfig"] = response.modify_args

        # Truncate additionalContext to SDK limit before emitting
        if "additionalContext" in hook_specific:
            hook_specific["additionalContext"] = truncate_additional_context(
                hook_specific["additionalContext"]
            )

        # Only add hookSpecificOutput if there's content
        if hook_specific:
            result["hookSpecificOutput"] = hook_specific

        # Add system message if present (user-visible notification)
        if response.system_message:
            result["systemMessage"] = response.system_message

        return result

    def handle_native(
        self, native_event: dict[str, Any], hook_manager: "HookManager"
    ) -> dict[str, Any]:
        """Main entry point for HTTP endpoint.

        Translates native Gemini CLI event, processes through HookManager,
        and returns response in Gemini's expected format.

        Args:
            native_event: Raw payload from Gemini CLI's hook_dispatcher.py
            hook_manager: HookManager instance for processing.

        Returns:
            Response dict in Gemini CLI's expected format.
        """
        # Translate to unified HookEvent
        hook_event = self.translate_to_hook_event(native_event)

        # Get original hook type for response formatting
        hook_type = native_event.get("hook_type", "")
        if not hook_type:
            hook_type = native_event.get("input_data", {}).get("hook_event_name", "")

        # Process through HookManager
        hook_response = hook_manager.handle(hook_event)

        # Translate response back to Gemini format
        return self.translate_from_hook_response(hook_response, hook_type=hook_type)

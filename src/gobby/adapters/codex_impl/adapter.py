"""
Codex adapter implementations.

Contains the main adapter classes for Codex CLI integration:
- CodexAdapter: Main adapter for app-server mode (programmatic control)
- CodexHooksAdapter: Adapter for hooks.json lifecycle events (SessionStart, PreToolUse, etc.)

Extracted from codex.py as part of Phase 3 Strangler Fig decomposition.
"""

from __future__ import annotations

import json
import logging
import platform
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.adapters.base import BaseAdapter
from gobby.adapters.codex_impl.client import (
    CodexAppServerClient,
)
from gobby.adapters.codex_impl.types import (
    CodexThread,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

logger = logging.getLogger(__name__)


# =============================================================================
# Shared Utilities
# =============================================================================


def _get_daemon_machine_id() -> str | None:
    """Get machine ID from the daemon's centralized utility.

    This adapter runs in the daemon process, so we use the centralized
    machine_id management from utils.machine_id.
    """
    from gobby.utils.machine_id import get_machine_id

    return get_machine_id()


def _get_machine_id() -> str:
    """Generate a machine identifier.

    Used by Codex adapters when no machine_id is provided.
    """
    node = platform.node()
    if node:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, node))
    return str(uuid.uuid4())


# =============================================================================
# App-Server Adapter (for programmatic control)
# =============================================================================


class CodexAdapter(BaseAdapter):
    """Adapter for Codex CLI session tracking via app-server events.

    This adapter translates Codex app-server events to unified HookEvent
    for session tracking. It can operate in two modes:

    1. Integrated mode (recommended): Attach to existing CodexAppServerClient
       - Call attach_to_client(codex_client) with the existing client
       - Events are forwarded from the client's notification handlers

    2. Standalone mode: Use without CodexAppServerClient
       - Only provides translation methods for events received externally
       - No subprocess management (use CodexAppServerClient for that)

    Lifecycle (integrated mode):
    - attach_to_client(codex_client) registers notification handlers
    - Events processed through HookManager for session registration
    - detach_from_client() removes handlers
    """

    source = SessionSource.CODEX

    # Event type mapping: Codex app-server methods -> unified HookEventType
    EVENT_MAP: dict[str, HookEventType] = {
        "thread/started": HookEventType.SESSION_START,
        "thread/archive": HookEventType.SESSION_END,
        "thread/closed": HookEventType.SESSION_END,  # Unsubscribe = end
        "turn/started": HookEventType.BEFORE_AGENT,
        "turn/completed": HookEventType.AFTER_AGENT,
        # Approval requests map to BEFORE_TOOL
        "item/commandExecution/requestApproval": HookEventType.BEFORE_TOOL,
        "item/fileChange/requestApproval": HookEventType.BEFORE_TOOL,
        # Completed items map to AFTER_TOOL
        "item/completed": HookEventType.AFTER_TOOL,
    }

    # Tool name mapping: Codex tool names -> canonical CC-style names
    # Codex uses different tool names - normalize to Claude Code conventions
    # so block_tools rules work across CLIs
    TOOL_MAP: dict[str, str] = {
        # File operations
        "read_file": "Read",
        "ReadFile": "Read",
        "write_file": "Write",
        "WriteFile": "Write",
        "edit_file": "Edit",
        "EditFile": "Edit",
        # Shell
        "run_shell_command": "Bash",
        "RunShellCommand": "Bash",
        "commandExecution": "Bash",
        # Search
        "glob": "Glob",
        "grep": "Grep",
        "GlobTool": "Glob",
        "GrepTool": "Grep",
    }

    # Item types that represent tool operations
    TOOL_ITEM_TYPES = {"commandExecution", "fileChange", "mcpToolCall"}

    # Events we want to listen for session tracking
    SESSION_TRACKING_EVENTS = [
        "thread/started",
        "thread/closed",
        "turn/started",
        "turn/completed",
        "item/completed",
    ]

    def __init__(self, hook_manager: HookManager | None = None):
        """Initialize the Codex adapter.

        Args:
            hook_manager: Reference to HookManager for event processing.
        """
        self._hook_manager = hook_manager
        self._codex_client: CodexAppServerClient | None = None
        self._attached = False
        self._machine_id: str | None = None

    @staticmethod
    def is_codex_available() -> bool:
        """Check if Codex CLI is installed and available.

        Returns:
            True if `codex` command is found in PATH.
        """
        import shutil

        return shutil.which("codex") is not None

    def _get_machine_id(self) -> str | None:
        """Get machine ID with caching and daemon fallback."""
        if self._machine_id:
            return self._machine_id

        # Try daemon first
        self._machine_id = _get_daemon_machine_id()

        # Fallback to generated if daemon not available
        if not self._machine_id:
            self._machine_id = _get_machine_id()

        return self._machine_id

    def normalize_tool_name(self, codex_tool_name: str) -> str:
        """Normalize Codex tool name to canonical CC-style format.

        This ensures block_tools rules work consistently across CLIs.

        Args:
            codex_tool_name: Tool name from Codex CLI.

        Returns:
            Normalized tool name (e.g., "Bash", "Read", "Write", "Edit").
        """
        return self.TOOL_MAP.get(codex_tool_name, codex_tool_name)

    def attach_to_client(self, codex_client: CodexAppServerClient) -> None:
        """Attach to an existing CodexAppServerClient for event handling.

        Registers notification handlers on the client to receive session
        tracking events. This is the preferred integration mode.

        Args:
            codex_client: The CodexAppServerClient to attach to.
        """
        if self._attached:
            logger.warning("CodexAdapter already attached to a client")
            return

        self._codex_client = codex_client

        # Register handlers for session tracking events
        for method in self.SESSION_TRACKING_EVENTS:
            codex_client.add_notification_handler(method, self._handle_notification)

        # Register approval handler for bidirectional tool blocking
        codex_client.register_approval_handler(self.handle_approval_request)

        self._attached = True
        logger.debug("CodexAdapter attached to CodexAppServerClient")

    def detach_from_client(self) -> None:
        """Detach from the CodexAppServerClient.

        Removes notification handlers. Call this before disposing the adapter.
        """
        if not self._attached or not self._codex_client:
            return

        # Remove handlers
        for method in self.SESSION_TRACKING_EVENTS:
            self._codex_client.remove_notification_handler(method, self._handle_notification)

        self._codex_client = None
        self._attached = False
        logger.debug("CodexAdapter detached from CodexAppServerClient")

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        """Handle notification from CodexAppServerClient.

        This is the callback registered with the client for session tracking events.
        """
        try:
            hook_event = self.translate_to_hook_event({"method": method, "params": params})

            if hook_event and self._hook_manager:
                # Process through HookManager (fire-and-forget for notifications)
                self._hook_manager.handle(hook_event)
                logger.debug(f"Processed Codex event: {method} -> {hook_event.event_type}")
        except Exception as e:
            logger.error(f"Error handling Codex notification {method}: {e}")

    async def handle_approval_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle an incoming approval request from Codex.

        Translates the approval request to a HookEvent, processes it through
        HookManager, and returns the decision in Codex format.

        Args:
            method: JSON-RPC method (e.g., "item/commandExecution/requestApproval")
            params: Request parameters from Codex.

        Returns:
            Decision dict: {"decision": "accept"} or {"decision": "decline"}
        """
        hook_event = self._translate_approval_event(method, params)
        if not hook_event:
            # Unknown method - default to accept
            return {"decision": "accept"}

        if not self._hook_manager:
            # No hook manager - default to accept
            return {"decision": "accept"}

        try:
            hook_response = self._hook_manager.handle(hook_event)
            return self.translate_from_hook_response(hook_response)
        except Exception as e:
            logger.error(f"Error processing approval request {method}: {e}")
            return {"decision": "accept"}

    def _translate_approval_event(self, method: str, params: dict[str, Any]) -> HookEvent | None:
        """Translate approval request to HookEvent."""
        if method not in self.EVENT_MAP:
            logger.debug(f"Unknown approval method: {method}")
            return None

        thread_id = params.get("threadId", "")
        item_id = params.get("itemId", "")

        # Determine tool name from method and normalize to CC-style
        if "commandExecution" in method:
            original_tool = "commandExecution"
            tool_name = self.normalize_tool_name(original_tool)  # -> "Bash"
            tool_input = params.get("parsedCmd", params.get("command", ""))
        elif "fileChange" in method:
            original_tool = "fileChange"
            tool_name = "Write"  # File changes are writes
            tool_input = params.get("changes", [])
        else:
            original_tool = "unknown"
            tool_name = "unknown"
            tool_input = params

        from gobby.hooks.normalization import normalize_tool_fields

        data = {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "item_id": item_id,
            "turn_id": params.get("turnId", ""),
            "reason": params.get("reason"),
            "risk": params.get("risk"),
        }
        normalize_tool_fields(data)

        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=thread_id,
            source=self.source,
            timestamp=datetime.now(UTC),
            machine_id=self._get_machine_id(),
            data=data,
            metadata={
                "requires_response": True,
                "item_id": item_id,
                "approval_method": method,
                "original_tool_name": original_tool,
                "normalized_tool_name": tool_name,
            },
        )

    def translate_to_hook_event(self, native_event: dict[str, Any]) -> HookEvent | None:
        """Convert Codex app-server event to unified HookEvent.

        Codex events come as JSON-RPC notifications:
        {
            "method": "thread/started",
            "params": {
                "thread": {"id": "thr_123", "preview": "...", ...}
            }
        }

        Args:
            native_event: JSON-RPC notification with method and params.

        Returns:
            Unified HookEvent, or None for unsupported events.
        """
        method = native_event.get("method", "")
        params = native_event.get("params", {})

        # Handle different event types
        if method == "thread/started":
            thread = params.get("thread", {})
            data = {
                "preview": thread.get("preview", ""),
                "model_provider": thread.get("modelProvider", ""),
            }
            transcript_path = thread.get("path")
            if transcript_path:
                data["transcript_path"] = transcript_path

            cwd = params.get("cwd")
            if isinstance(cwd, str) and cwd:
                data["cwd"] = cwd

            terminal_context = params.get("terminal_context")
            if isinstance(terminal_context, dict) and terminal_context:
                data["terminal_context"] = terminal_context

            return HookEvent(
                event_type=HookEventType.SESSION_START,
                session_id=thread.get("id", ""),
                source=self.source,
                timestamp=self._parse_timestamp(thread.get("createdAt")),
                machine_id=self._get_machine_id(),
                cwd=cwd if isinstance(cwd, str) else None,
                data=data,
            )

        if method in ("thread/archive", "thread/closed"):
            return HookEvent(
                event_type=HookEventType.SESSION_END,
                session_id=params.get("threadId", ""),
                source=self.source,
                timestamp=datetime.now(UTC),
                machine_id=self._get_machine_id(),
                data=params,
            )

        if method == "turn/started":
            turn = params.get("turn", {})
            return HookEvent(
                event_type=HookEventType.BEFORE_AGENT,
                session_id=params.get("threadId", turn.get("id", "")),
                source=self.source,
                timestamp=datetime.now(UTC),
                machine_id=self._get_machine_id(),
                data={
                    "turn_id": turn.get("id", ""),
                    "status": turn.get("status", ""),
                },
            )

        if method == "turn/completed":
            turn = params.get("turn", {})
            return HookEvent(
                event_type=HookEventType.AFTER_AGENT,
                session_id=params.get("threadId", turn.get("id", "")),
                source=self.source,
                timestamp=datetime.now(UTC),
                machine_id=self._get_machine_id(),
                data={
                    "turn_id": turn.get("id", ""),
                    "status": turn.get("status", ""),
                    "error": turn.get("error"),
                },
            )

        if method == "item/completed":
            item = params.get("item", {})
            item_type = item.get("type", "")

            # contextCompaction items map to PRE_COMPACT (not AFTER_TOOL)
            if item_type == "contextCompaction":
                return HookEvent(
                    event_type=HookEventType.PRE_COMPACT,
                    session_id=params.get("threadId", ""),
                    source=self.source,
                    timestamp=datetime.now(UTC),
                    machine_id=self._get_machine_id(),
                    data={
                        "trigger": "auto",
                        "item_id": item.get("id", ""),
                        "item_type": item_type,
                    },
                )

            # Only translate tool-related items
            if item_type in self.TOOL_ITEM_TYPES:
                from gobby.hooks.normalization import normalize_tool_fields

                item_data: dict[str, Any] = {
                    "item_id": item.get("id", ""),
                    "item_type": item_type,
                    "status": item.get("status", ""),
                }
                normalize_tool_fields(item_data)

                return HookEvent(
                    event_type=HookEventType.AFTER_TOOL,
                    session_id=params.get("threadId", ""),
                    source=self.source,
                    timestamp=datetime.now(UTC),
                    machine_id=self._get_machine_id(),
                    data=item_data,
                )

        # Unknown/unsupported event
        logger.debug(f"Unsupported Codex event: {method}")
        return None

    def translate_from_hook_response(
        self, response: HookResponse, hook_type: str | None = None
    ) -> dict[str, Any]:
        """Convert HookResponse to Codex response format with context injection.

        Unlike Claude/Gemini which use hookSpecificOutput.additionalContext,
        Codex injects context via the `instructions` field at turn start.
        This method builds a `context` string from HookResponse metadata
        for the caller to pass to start_turn(context_prefix=...).

        Args:
            response: Unified HookResponse.
            hook_type: Original Codex method (unused, kept for interface).

        Returns:
            Dict with decision and optional context field.
        """
        # Map HookResponse decision to Codex rich approval format
        if response.decision == "deny":
            decision = "decline"
        elif response.decision == "block":
            decision = "cancel"
        elif response.auto_approve:
            decision = "acceptForSession"
        elif response.metadata.get("exec_policy_amendment"):
            decision = "acceptWithExecpolicyAmendment"
        else:
            decision = "accept"

        result: dict[str, Any] = {"decision": decision}

        # Include amendment payload for policy updates
        if decision == "acceptWithExecpolicyAmendment":
            result["execPolicyAmendment"] = response.metadata["exec_policy_amendment"]

        # Build context parts from workflow context and session metadata
        context_parts: list[str] = []

        # Add workflow-injected context (from inject_context action)
        if response.context:
            context_parts.append(response.context)

        # Add session metadata context
        if response.metadata:
            session_id = response.metadata.get("session_id")
            session_ref = response.metadata.get("session_ref")
            external_id = response.metadata.get("external_id")
            is_first_hook = response.metadata.get("_first_hook_for_session", False)

            if session_id:
                if is_first_hook:
                    # First hook: inject full metadata
                    context_lines = []
                    if session_ref:
                        context_lines.append(f"Gobby Session ID: {session_ref} ({session_id})")
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
                    for key in [
                        "terminal_tmux_pane",
                    ]:
                        if response.metadata.get(key):
                            friendly_name = key.replace("terminal_", "").replace("_", " ")
                            context_lines.append(f"{friendly_name}: {response.metadata[key]}")
                    context_parts.append("\n".join(context_lines))

        # Add context to result if we have any
        if context_parts:
            result["context"] = "\n\n".join(context_parts)

        return result

    def _parse_timestamp(self, unix_ts: int | float | None) -> datetime:
        """Parse Unix timestamp to datetime.

        Args:
            unix_ts: Unix timestamp (seconds).

        Returns:
            Timezone-aware datetime object, or now(UTC) if parsing fails.
        """
        if unix_ts:
            try:
                return datetime.fromtimestamp(unix_ts, tz=UTC)
            except (ValueError, OSError):
                pass
        return datetime.now(UTC)

    async def sync_existing_sessions(self) -> int:
        """Sync existing Codex threads to platform sessions.

        Uses the attached CodexAppServerClient to list threads and registers
        them as sessions via HookManager.

        Requires:
        - CodexAdapter attached to a CodexAppServerClient
        - CodexAppServerClient is connected
        - HookManager is set

        Returns:
            Number of threads synced.
        """
        if not self._hook_manager:
            logger.warning("No hook_manager - cannot sync sessions")
            return 0

        if not self._codex_client:
            logger.warning("No CodexAppServerClient attached - cannot sync sessions")
            return 0

        if not self._codex_client.is_connected:
            logger.warning("CodexAppServerClient not connected - cannot sync sessions")
            return 0

        try:
            # Use CodexAppServerClient to list threads
            all_threads: list[CodexThread] = []
            cursor = None

            while True:
                threads, next_cursor = await self._codex_client.list_threads(
                    cursor=cursor, limit=100
                )
                all_threads.extend(threads)

                if not next_cursor:
                    break
                cursor = next_cursor

            synced = 0
            for thread in all_threads:
                try:
                    event = HookEvent(
                        event_type=HookEventType.SESSION_START,
                        session_id=thread.id,
                        source=self.source,
                        timestamp=self._parse_timestamp(thread.created_at),
                        machine_id=self._get_machine_id(),
                        data={
                            "preview": thread.preview,
                            "model_provider": thread.model_provider,
                            "synced_from_existing": True,
                        },
                    )
                    self._hook_manager.handle(event)
                    synced += 1
                except Exception as e:
                    logger.error(f"Failed to sync thread {thread.id}: {e}")

            logger.debug(f"Synced {synced} existing Codex threads")
            return synced

        except Exception as e:
            logger.error(f"Failed to sync existing sessions: {e}")
            return 0


# =============================================================================
# Notify Adapter (for installed hooks via `gobby install --codex`)
# =============================================================================


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
        """Convert Codex hooks.json payload to HookEvent.

        The payload structure matches Claude Code's dispatcher format:
        {
            "hook_type": "SessionStart",
            "input_data": {
                "session_id": "...",
                "cwd": "/path/to/project",
                "model": "...",
                ...
            },
            "source": "codex"
        }
        """
        hook_type = native_event.get("hook_type", "")
        input_data = native_event.get("input_data") or {}

        event_type = self.EVENT_MAP.get(hook_type)
        if event_type is None:
            logger.warning(f"Codex hooks: unsupported hook type '{hook_type}'")
            return None

        session_id = input_data.get("session_id", "")

        # Normalize event data (same as Claude — reuse shared normalization)
        from gobby.hooks.normalization import normalize_tool_fields

        normalized_data = normalize_tool_fields(dict(input_data))

        # Check for failure on PostToolUse
        is_failure = normalized_data.get("is_error", False)
        metadata = {"is_failure": is_failure} if is_failure else {}
        original_tool_name = normalized_data.pop("_original_tool_name", None)
        if original_tool_name:
            metadata["original_tool_name"] = original_tool_name
            metadata["normalized_tool_name"] = normalized_data.get("tool_name")

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
        """Convert HookResponse to Codex hooks.json expected format.

        Codex hooks share some top-level fields with Claude Code, but PreToolUse
        requires a Codex-specific ``hookSpecificOutput.permissionDecision``
        contract for block semantics.
        """
        from gobby.llm.sdk_utils import compress_and_truncate

        hook_event_name = hook_type or "Unknown"

        # Codex CLI 0.120.0 rejects ``updatedInput`` and ``permissionDecision=allow``
        # for PreToolUse hooks. When Gobby wants to rewrite a tool call, block the
        # current execution and tell the model exactly how to retry instead.
        has_retry_signal = bool(
            response.auto_approve or response.reason or response.context or response.system_message
        )
        if response.modified_input and hook_event_name == "PreToolUse" and has_retry_signal:
            retry_reason = (
                response.reason
                or "Retry the tool call with the corrected input from the hook message."
            )
            retry_parts: list[str] = []
            if response.system_message:
                retry_parts.append(response.system_message)
            if response.context:
                retry_parts.append(response.context)
            retry_parts.append(
                "Retry this tool call with the corrected input below:\n"
                f"{json.dumps(response.modified_input, indent=2, sort_keys=True)}"
            )
            result: dict[str, Any] = {
                "decision": "block",
                "reason": retry_reason,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": retry_reason,
                },
            }
            result["systemMessage"] = compress_and_truncate("\n\n".join(retry_parts))[0]
            return result

        if response.decision in ("deny", "block"):
            if hook_event_name == "PreToolUse":
                result: dict[str, Any] = {
                    "decision": "block",
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                    },
                }
                if response.reason:
                    result["reason"] = response.reason
                    result["hookSpecificOutput"]["permissionDecisionReason"] = response.reason

                system_parts: list[str] = []
                if response.system_message:
                    system_parts.append(response.system_message)
                if response.context:
                    system_parts.append(response.context)
                if system_parts:
                    result["systemMessage"] = compress_and_truncate("\n\n".join(system_parts))[0]
                return result

            result = {"continue": False, "decision": "block"}
            if response.reason:
                result["reason"] = response.reason
            return result

        result: dict[str, Any] = {"continue": True}

        # Build additionalContext from all context sources
        context_parts: list[str] = []

        # Workflow-injected context (inject_context action)
        if response.context:
            context_parts.append(response.context)

        # Route system_message by event type:
        # - systemMessage-only events (PreToolUse, Stop): visible systemMessage
        # - SessionStart: BOTH systemMessage (visible banner) AND additionalContext (model-facing)
        # - UserPromptSubmit, PostToolUse: additionalContext only (hidden from user)
        if response.system_message:
            if hook_event_name in self.SYSTEM_MESSAGE_ONLY_EVENTS:
                result["systemMessage"] = response.system_message
            else:
                # Always feed to model via additionalContext
                context_parts.insert(0, response.system_message)
                # SessionStart banner should also be visible to the user
                if hook_event_name == "SessionStart":
                    result["systemMessage"] = response.system_message

        # Session metadata (Gobby session ID, terminal context, etc.)
        if response.metadata:
            gobby_session_id = response.metadata.get("session_id")
            session_ref = response.metadata.get("session_ref")
            external_id = response.metadata.get("external_id")
            is_first_hook = response.metadata.get("_first_hook_for_session", False)

            if gobby_session_id:
                if is_first_hook:
                    context_lines = []
                    if session_ref:
                        context_lines.append(
                            f"Gobby Session ID: {session_ref} ({gobby_session_id})"
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
                    task_id = response.metadata.get("task_id")
                    if task_id:
                        context_lines.append(
                            f"Assigned Task: {task_id}"
                            " (use this for task operations, NOT the session ID above)"
                        )
                    if response.metadata.get("terminal_term_program"):
                        context_lines.append(
                            f"terminal: {response.metadata['terminal_term_program']}"
                        )
                    if response.metadata.get("terminal_parent_pid"):
                        context_lines.append(
                            f"parent_pid: {response.metadata['terminal_parent_pid']}"
                        )
                    for key in [
                        "terminal_tmux_pane",
                    ]:
                        if response.metadata.get(key):
                            friendly_name = key.replace("terminal_", "").replace("_", " ")
                            context_lines.append(f"{friendly_name}: {response.metadata[key]}")
                    context_parts.append("\n".join(context_lines))

        # Build hookSpecificOutput or systemMessage based on event type.
        # PreToolUse/Stop only accept systemMessage — additionalContext is rejected.
        if context_parts:
            combined_context = compress_and_truncate("\n\n".join(context_parts))[0]
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


# Backward-compatible alias for old notify adapter references
CodexNotifyAdapter = CodexHooksAdapter


__all__ = [
    "CodexAdapter",
    "CodexHooksAdapter",
    "CodexNotifyAdapter",
]

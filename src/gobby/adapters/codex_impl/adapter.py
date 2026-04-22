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
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.adapters.base import (
    BaseAdapter,
    build_first_hook_session_metadata_lines,
    normalize_adapter_response_reason,
    system_message_has_session_banner,
)
from gobby.adapters.codex_impl.client import (
    CodexAppServerClient,
)
from gobby.adapters.codex_impl.item_normalization import (
    TOOL_ITEM_TYPES as _SHARED_TOOL_ITEM_TYPES,
    build_tool_event_data as _shared_build_tool_event_data,
    compose_mcp_tool_name as _shared_compose_mcp_tool_name,
    extract_completed_item_payload as _shared_extract_completed_item_payload,
    looks_like_tool_item as _shared_looks_like_tool_item,
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
        "item/mcpToolCall/requestApproval": HookEventType.BEFORE_TOOL,
        "mcpServer/elicitation/request": HookEventType.BEFORE_TOOL,
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

    # Safe MCP registry discovery calls should never be blocked behind
    # Codex approval prompts in web chat. They only inspect the proxy.
    SAFE_MCP_PROXY_TOOLS: set[str] = {
        "mcp__gobby__list_mcp_servers",
        "mcp__gobby__list_tools",
        "mcp__gobby__get_tool_schema",
        "mcp__gobby__recommend_tools",
        "mcp__gobby__search_tools",
    }

    # UI-only canvas calls are safe because they only present/update
    # browser surfaces; they do not mutate repo or system state.
    SAFE_CANVAS_CALL_TOOLS: set[str] = {
        "render_surface",
        "update_surface",
        "close_canvas",
        "wait_for_interaction",
        "canvas_present",
        "show_file",
    }

    # Item types that represent tool operations
    TOOL_ITEM_TYPES = _SHARED_TOOL_ITEM_TYPES

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
    def _compose_mcp_tool_name(server_name: str, tool_name: str) -> str:
        """Return the canonical MCP tool name used by shared hook logic."""
        return _shared_compose_mcp_tool_name(server_name, tool_name)

    @staticmethod
    def _extract_mcp_tool_name_from_message(message: Any) -> str | None:
        """Best-effort parse of the tool name embedded in Codex MCP prompts."""
        if not isinstance(message, str):
            return None
        match = re.search(r'run tool "([^"]+)"', message)
        if not match:
            return None
        tool_name = match.group(1).strip()
        return tool_name or None

    @staticmethod
    def _translate_mcp_elicitation_response(response: HookResponse | None = None) -> dict[str, Any]:
        """Translate a hook decision into Codex MCP elicitation response shape."""
        action = "accept"
        if response is not None:
            if response.decision == "deny":
                action = "decline"
            elif response.decision == "block":
                action = "cancel"
        return {"action": action, "content": None, "_meta": None}

    @staticmethod
    def _fail_closed_approval_response(method: str) -> dict[str, Any]:
        """Return the safest denial shape when approval handling is unavailable."""
        if method == "mcpServer/elicitation/request":
            return {"action": "cancel", "content": None, "_meta": None}
        return {"decision": "decline"}

    @classmethod
    def _extract_completed_item_payload(cls, params: dict[str, Any]) -> dict[str, Any]:
        """Return the best-effort tool item payload from an item/completed event."""
        return _shared_extract_completed_item_payload(params)

    @classmethod
    def _looks_like_tool_item(cls, item: dict[str, Any]) -> bool:
        """Identify completed Codex items that represent tool execution."""
        return _shared_looks_like_tool_item(item)

    def _build_completed_tool_data(self, item: dict[str, Any]) -> dict[str, Any]:
        """Normalize a completed Codex tool item into hook event data."""
        return _shared_build_tool_event_data(item, tool_name_map=self.TOOL_MAP)

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
            logger.warning("Approval request %s could not be translated; failing closed", method)
            return self._fail_closed_approval_response(method)

        if not self._hook_manager:
            logger.warning("Approval request %s has no hook manager; failing closed", method)
            return self._fail_closed_approval_response(method)

        is_safe_auto_approved = self._is_safe_auto_approved_tool(hook_event)

        try:
            hook_response = self._hook_manager.handle(hook_event)
        except Exception as e:
            logger.error(f"Error processing approval request {method}: {e}")
            if is_safe_auto_approved:
                if method == "mcpServer/elicitation/request":
                    return self._translate_mcp_elicitation_response()
                return {"decision": "accept"}
            return self._fail_closed_approval_response(method)

        if is_safe_auto_approved:
            if method == "mcpServer/elicitation/request":
                return self._translate_mcp_elicitation_response()
            return {"decision": "accept"}

        if method == "mcpServer/elicitation/request":
            return self._translate_mcp_elicitation_response(hook_response)
        return self.translate_from_hook_response(hook_response)

    def _is_safe_auto_approved_tool(self, hook_event: HookEvent) -> bool:
        """Return True for safe MCP discovery/UI-only tool calls."""
        tool_name = hook_event.data.get("tool_name")
        if tool_name in self.SAFE_MCP_PROXY_TOOLS:
            return True

        mcp_server = hook_event.data.get("mcp_server")
        mcp_tool = hook_event.data.get("mcp_tool")
        if mcp_server == "gobby-canvas" and mcp_tool in self.SAFE_CANVAS_CALL_TOOLS:
            return True

        if tool_name != "mcp__gobby__call_tool":
            return False

        raw_input = hook_event.data.get("tool_input") or hook_event.data.get("toolArgs") or {}
        if not isinstance(raw_input, dict):
            return False
        return (
            raw_input.get("server_name") == "gobby-canvas"
            and raw_input.get("tool_name") in self.SAFE_CANVAS_CALL_TOOLS
        )

    def _translate_approval_event(self, method: str, params: dict[str, Any]) -> HookEvent | None:
        """Translate approval request to HookEvent."""
        if method not in self.EVENT_MAP:
            logger.debug(f"Unknown approval method: {method}")
            return None

        if method == "mcpServer/elicitation/request":
            meta = params.get("_meta")
            if not isinstance(meta, dict) or meta.get("codex_approval_kind") != "mcp_tool_call":
                logger.debug("Ignoring unsupported Codex elicitation request: %s", method)
                return None

            server_name = params.get("serverName")
            tool_name = self._extract_mcp_tool_name_from_message(params.get("message"))
            if not isinstance(server_name, str) or not server_name or not tool_name:
                logger.debug("Unable to derive MCP tool identity from elicitation request")
                return None

            original_tool = _shared_compose_mcp_tool_name(server_name, tool_name)
            tool_params = meta.get("tool_params")
            tool_input = tool_params if isinstance(tool_params, dict) else {}
            data = {
                "item_id": params.get("elicitationId", ""),
                "item_type": "mcpToolCall",
                "turn_id": params.get("turnId", ""),
                "tool_name": original_tool,
                "tool_input": tool_input,
                "server_name": server_name,
                "message": params.get("message"),
            }

            from gobby.hooks.normalization import normalize_tool_fields

            normalize_tool_fields(data)

            return HookEvent(
                event_type=HookEventType.BEFORE_TOOL,
                session_id=params.get("threadId", ""),
                source=self.source,
                timestamp=datetime.now(UTC),
                machine_id=self._get_machine_id(),
                data=data,
                metadata={
                    "requires_response": True,
                    "item_id": data["item_id"],
                    "approval_method": method,
                    "original_tool_name": original_tool,
                    "normalized_tool_name": data.get("tool_name", original_tool),
                },
            )

        thread_id = params.get("threadId", "")
        item_type = method.removeprefix("item/").removesuffix("/requestApproval")

        approval_payload: dict[str, Any] = {}
        nested_payload = params.get(item_type)
        if isinstance(nested_payload, dict):
            approval_payload.update(nested_payload)
        approval_payload.update(params)

        item_id = approval_payload.get("itemId", params.get("itemId", ""))

        data = {
            "item_id": item_id,
            "item_type": item_type,
            "turn_id": approval_payload.get("turnId", params.get("turnId", "")),
            "reason": approval_payload.get("reason"),
            "risk": approval_payload.get("risk"),
        }

        # Determine tool name and payload from the Codex item type.
        if item_type == "commandExecution":
            original_tool = "commandExecution"
            tool_name = self.normalize_tool_name(original_tool)
            data["tool_name"] = tool_name
            data["tool_input"] = approval_payload.get(
                "parsedCmd", approval_payload.get("command", "")
            )
        elif item_type == "fileChange":
            original_tool = "fileChange"
            tool_name = "Write"
            data["tool_name"] = tool_name
            data["tool_input"] = approval_payload.get("changes", [])
        elif item_type == "mcpToolCall":
            original_tool = (
                approval_payload.get("tool_name")
                or approval_payload.get("toolName")
                or approval_payload.get("name")
                or item_type
            )
            tool_name = self.normalize_tool_name(original_tool)
            data["tool_name"] = tool_name
            if "tool_input" in approval_payload:
                data["tool_input"] = approval_payload["tool_input"]
            elif "toolArgs" in approval_payload:
                data["toolArgs"] = approval_payload["toolArgs"]
            elif "arguments" in approval_payload:
                data["toolArgs"] = approval_payload["arguments"]
            elif "input" in approval_payload:
                data["tool_input"] = approval_payload["input"]
            elif "params" in approval_payload:
                data["tool_input"] = approval_payload["params"]
            else:
                data["tool_input"] = {}
        else:
            original_tool = "unknown"
            tool_name = "unknown"
            data["tool_name"] = tool_name
            data["tool_input"] = approval_payload

        from gobby.hooks.normalization import normalize_tool_fields

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
        event_cwd = params.get("cwd") if isinstance(params.get("cwd"), str) else None

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
                    "prompt": params.get("prompt", ""),
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
            item = self._extract_completed_item_payload(params)
            item_type = item.get("type") or item.get("itemType") or ""

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
            if self._looks_like_tool_item(item):
                item_data = self._build_completed_tool_data(item)

                return HookEvent(
                    event_type=HookEventType.AFTER_TOOL,
                    session_id=params.get("threadId", ""),
                    source=self.source,
                    timestamp=datetime.now(UTC),
                    machine_id=self._get_machine_id(),
                    cwd=event_cwd,
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
        raw_tool_name = normalized_data.get("tool_name")
        if isinstance(raw_tool_name, str):
            normalized_tool_name = CodexAdapter.TOOL_MAP.get(raw_tool_name, raw_tool_name)
            if normalized_tool_name != raw_tool_name:
                normalized_data.setdefault("_original_tool_name", raw_tool_name)
                normalized_data["tool_name"] = normalized_tool_name

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
        normalized_reason = normalize_adapter_response_reason(
            response,
            adapter_name=self.__class__.__name__,
            hook_type=hook_type,
            logger=logger,
        )

        # Codex CLI 0.120.0 rejects ``updatedInput`` and ``permissionDecision=allow``
        # for PreToolUse hooks. When Gobby wants to rewrite a tool call, block the
        # current execution and tell the model exactly how to retry instead.
        has_retry_signal = bool(
            response.auto_approve
            or normalized_reason
            or response.context
            or response.system_message
        )
        if response.modified_input and hook_event_name == "PreToolUse" and has_retry_signal:
            retry_reason = (
                normalized_reason
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
            retry_result: dict[str, Any] = {
                "decision": "block",
                "reason": retry_reason,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": retry_reason,
                },
            }
            retry_result["systemMessage"] = compress_and_truncate("\n\n".join(retry_parts))[0]
            return retry_result

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
                    deny_result["systemMessage"] = compress_and_truncate("\n\n".join(system_parts))[
                        0
                    ]
                return deny_result

            block_result: dict[str, Any] = {"continue": False, "decision": "block"}
            if normalized_reason:
                block_result["reason"] = normalized_reason
            return block_result

        result: dict[str, Any] = {"continue": True}

        # Build additionalContext from all context sources
        context_parts: list[str] = []

        # Workflow-injected context (inject_context action)
        if response.context:
            context_parts.append(response.context)

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
                context_parts.insert(0, response.system_message)

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

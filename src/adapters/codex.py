"""Codex CLI adapter for session management via app-server.

This adapter integrates with the existing CodexAppServerClient to provide
session tracking for Codex threads. Unlike Claude/Gemini adapters which handle
HTTP hook requests, CodexAdapter:
- Registers notification handlers on CodexAppServerClient
- Translates Codex events to unified HookEvent
- Processes events through HookManager for session registration

Integration Modes:
1. Standalone mode: CodexAdapter spawns its own app-server subprocess
2. Integrated mode: Uses existing CodexAppServerClient (preferred)

Codex Core Primitives:
- Thread: A conversation between user and Codex agent (maps to session)
- Turn: One turn of conversation (user input -> agent response)
- Item: User inputs and agent outputs within a turn

See: https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md
"""

import logging
import platform
import shutil
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from gobby.adapters.base import BaseAdapter
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

if TYPE_CHECKING:
    from gobby.adapters.codex_client import CodexAppServerClient
    from gobby.hooks.hook_manager import HookManager

logger = logging.getLogger(__name__)


class CodexAdapter(BaseAdapter):  # type: ignore[misc]
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
        "turn/started": HookEventType.BEFORE_AGENT,
        "turn/completed": HookEventType.AFTER_AGENT,
        # Approval requests map to BEFORE_TOOL
        "item/commandExecution/requestApproval": HookEventType.BEFORE_TOOL,
        "item/fileChange/requestApproval": HookEventType.BEFORE_TOOL,
        # Completed items map to AFTER_TOOL
        "item/completed": HookEventType.AFTER_TOOL,
    }

    # Item types that represent tool operations
    TOOL_ITEM_TYPES = {"commandExecution", "fileChange", "mcpToolCall"}

    # Events we want to listen for session tracking
    SESSION_TRACKING_EVENTS = [
        "thread/started",
        "turn/started",
        "turn/completed",
        "item/completed",
    ]

    def __init__(self, hook_manager: "HookManager | None" = None):
        """Initialize the Codex adapter.

        Args:
            hook_manager: Reference to HookManager for event processing.
        """
        self._hook_manager = hook_manager
        self._codex_client: CodexAppServerClient | None = None
        self._machine_id: str | None = None
        self._attached = False

    def _get_machine_id(self) -> str:
        """Get or generate a machine identifier.

        Returns:
            A stable machine identifier based on hostname.
        """
        if self._machine_id is None:
            node = platform.node()
            if node:
                self._machine_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, node))
            else:
                self._machine_id = str(uuid.uuid4())
        return self._machine_id

    @staticmethod
    def is_codex_available() -> bool:
        """Check if Codex CLI is installed and available.

        Returns:
            True if `codex` command is found in PATH.
        """
        return shutil.which("codex") is not None

    def attach_to_client(self, codex_client: "CodexAppServerClient") -> None:
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

    def _translate_approval_event(self, method: str, params: dict) -> HookEvent | None:
        """Translate approval request to HookEvent."""
        if method not in self.EVENT_MAP:
            logger.debug(f"Unknown approval method: {method}")
            return None

        thread_id = params.get("threadId", "")
        item_id = params.get("itemId", "")

        # Determine tool name from method
        if "commandExecution" in method:
            tool_name = "Bash"
            tool_input = params.get("parsedCmd", params.get("command", ""))
        elif "fileChange" in method:
            tool_name = "Write"
            tool_input = params.get("changes", [])
        else:
            tool_name = "unknown"
            tool_input = params

        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=thread_id,
            source=self.source,
            timestamp=datetime.now(),
            machine_id=self._get_machine_id(),
            data={
                "tool_name": tool_name,
                "tool_input": tool_input,
                "item_id": item_id,
                "turn_id": params.get("turnId", ""),
                "reason": params.get("reason"),
                "risk": params.get("risk"),
            },
            metadata={
                "requires_response": True,
                "item_id": item_id,
                "approval_method": method,
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
            return HookEvent(
                event_type=HookEventType.SESSION_START,
                session_id=thread.get("id", ""),
                source=self.source,
                timestamp=self._parse_timestamp(thread.get("createdAt")),
                machine_id=self._get_machine_id(),
                data={
                    "preview": thread.get("preview", ""),
                    "model_provider": thread.get("modelProvider", ""),
                },
            )

        if method == "thread/archive":
            return HookEvent(
                event_type=HookEventType.SESSION_END,
                session_id=params.get("threadId", ""),
                source=self.source,
                timestamp=datetime.now(),
                machine_id=self._get_machine_id(),
                data=params,
            )

        if method == "turn/started":
            turn = params.get("turn", {})
            return HookEvent(
                event_type=HookEventType.BEFORE_AGENT,
                session_id=params.get("threadId", turn.get("id", "")),
                source=self.source,
                timestamp=datetime.now(),
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
                timestamp=datetime.now(),
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

            # Only translate tool-related items
            if item_type in self.TOOL_ITEM_TYPES:
                return HookEvent(
                    event_type=HookEventType.AFTER_TOOL,
                    session_id=params.get("threadId", ""),
                    source=self.source,
                    timestamp=datetime.now(),
                    machine_id=self._get_machine_id(),
                    data={
                        "item_id": item.get("id", ""),
                        "item_type": item_type,
                        "status": item.get("status", ""),
                    },
                )

        # Unknown/unsupported event
        logger.debug(f"Unsupported Codex event: {method}")
        return None

    def translate_from_hook_response(
        self, response: HookResponse, hook_type: str | None = None
    ) -> dict[str, Any]:
        """Convert HookResponse to Codex approval response format.

        Codex expects approval responses as:
        {
            "decision": "accept" | "decline"
        }

        Args:
            response: Unified HookResponse.
            hook_type: Original Codex method (unused, kept for interface).

        Returns:
            Dict with decision field.
        """
        return {
            "decision": "accept" if response.decision != "deny" else "decline",
        }

    def _parse_timestamp(self, unix_ts: int | float | None) -> datetime:
        """Parse Unix timestamp to datetime.

        Args:
            unix_ts: Unix timestamp (seconds).

        Returns:
            datetime object, or now() if parsing fails.
        """
        if unix_ts:
            try:
                return datetime.fromtimestamp(unix_ts)
            except (ValueError, OSError):
                pass
        return datetime.now()

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
            all_threads = []
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

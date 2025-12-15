"""Codex CLI integration for gobby-daemon.

This module provides two integration modes for Codex CLI:

1. App-Server Mode (programmatic control):
   - CodexAppServerClient: Spawns `codex app-server` subprocess
   - CodexAdapter: Translates app-server events to HookEvent
   - Full control over threads, turns, and streaming events

2. Notify Mode (installed hooks via `gobby install --codex`):
   - CodexNotifyAdapter: Handles HTTP webhooks from Codex notify config
   - Fire-and-forget events on agent-turn-complete

Architecture:
    App-Server Mode:
        gobby-daemon
        └── CodexAppServerClient
            ├── Spawns: `codex app-server` (stdio subprocess)
            ├── Protocol: JSON-RPC 2.0 over stdin/stdout
            └── CodexAdapter (translates events to HookEvent)

    Notify Mode:
        Codex CLI
        └── notify script (installed by `gobby install --codex`)
            └── HTTP POST to /hooks/execute
                └── CodexNotifyAdapter (translates to HookEvent)

See: https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md
"""

from __future__ import annotations

import asyncio
import glob as glob_module
import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from gobby.adapters.base import BaseAdapter
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

logger = logging.getLogger(__name__)

# Codex session storage location
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


# =============================================================================
# App-Server Data Types
# =============================================================================


class CodexConnectionState(Enum):
    """Connection state for the Codex app-server."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class CodexThread:
    """Represents a Codex conversation thread."""

    id: str
    preview: str = ""
    model_provider: str = "openai"
    created_at: int = 0


@dataclass
class CodexTurn:
    """Represents a turn in a Codex conversation."""

    id: str
    thread_id: str
    status: str = "pending"
    items: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    usage: dict[str, int] | None = None


@dataclass
class CodexItem:
    """Represents an item (message, tool call, etc.) in a turn."""

    id: str
    type: str  # "reasoning", "agent_message", "command_execution", "user_message", etc.
    content: str = ""
    status: str = "pending"
    metadata: dict[str, Any] = field(default_factory=dict)


# Type alias for notification handlers
NotificationHandler = Callable[[str, dict[str, Any]], None]


# =============================================================================
# App-Server Client (Programmatic Control)
# =============================================================================


class CodexAppServerClient:
    """
    Client for the Codex app-server JSON-RPC protocol.

    Manages the subprocess lifecycle and provides async methods for:
    - Thread management (conversations)
    - Turn management (message exchanges)
    - Event streaming via notifications

    Example:
        async with CodexAppServerClient() as client:
            thread = await client.start_thread(cwd="/path/to/project")
            async for event in client.run_turn(thread.id, "Help me refactor"):
                print(event)
    """

    CLIENT_NAME = "gobby-daemon"
    CLIENT_TITLE = "Gobby Daemon"
    CLIENT_VERSION = "0.1.0"

    def __init__(
        self,
        codex_command: str = "codex",
        on_notification: NotificationHandler | None = None,
    ) -> None:
        """
        Initialize the Codex app-server client.

        Args:
            codex_command: Path to the codex binary (default: "codex")
            on_notification: Optional callback for all notifications
        """
        self._codex_command = codex_command
        self._on_notification = on_notification

        self._process: subprocess.Popen | None = None
        self._state = CodexConnectionState.DISCONNECTED
        self._request_id = 0
        self._request_id_lock = threading.Lock()

        # Pending requests waiting for responses
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._pending_requests_lock = threading.Lock()

        # Notification handlers by method
        self._notification_handlers: dict[str, list[NotificationHandler]] = {}

        # Reader task
        self._reader_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()

        # Thread tracking for session management
        self._threads: dict[str, CodexThread] = {}

    @property
    def state(self) -> CodexConnectionState:
        """Get current connection state."""
        return self._state

    @property
    def is_connected(self) -> bool:
        """Check if connected to app-server."""
        return self._state == CodexConnectionState.CONNECTED

    async def __aenter__(self) -> CodexAppServerClient:
        """Async context manager entry - starts the app-server."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Async context manager exit - stops the app-server."""
        await self.stop()

    async def start(self) -> None:
        """
        Start the Codex app-server subprocess and initialize connection.

        Raises:
            RuntimeError: If already connected or failed to start
        """
        if self._state == CodexConnectionState.CONNECTED:
            logger.warning("CodexAppServerClient already connected")
            return

        self._state = CodexConnectionState.CONNECTING
        logger.debug("Starting Codex app-server...")

        try:
            # Start the subprocess
            self._process = subprocess.Popen(
                [self._codex_command, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
            )

            # Start the reader task
            self._shutdown_event.clear()
            self._reader_task = asyncio.create_task(self._read_loop())

            # Send initialize request
            result = await self._send_request(
                "initialize",
                {
                    "clientInfo": {
                        "name": self.CLIENT_NAME,
                        "title": self.CLIENT_TITLE,
                        "version": self.CLIENT_VERSION,
                    }
                },
            )

            user_agent = result.get("userAgent", "unknown")
            logger.debug(f"Codex app-server initialized: {user_agent}")

            # Send initialized notification
            await self._send_notification("initialized", {})

            self._state = CodexConnectionState.CONNECTED
            logger.debug("Codex app-server connection established")

        except Exception as e:
            self._state = CodexConnectionState.ERROR
            logger.error(f"Failed to start Codex app-server: {e}", exc_info=True)
            await self.stop()
            raise RuntimeError(f"Failed to start Codex app-server: {e}") from e

    async def stop(self) -> None:
        """Stop the Codex app-server subprocess."""
        logger.debug("Stopping Codex app-server...")

        self._shutdown_event.set()

        # Cancel reader task
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        # Terminate process
        if self._process:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception as e:
                logger.warning(f"Error terminating Codex app-server: {e}")
                self._process.kill()
            finally:
                self._process = None

        # Cancel pending requests
        with self._pending_requests_lock:
            for future in self._pending_requests.values():
                if not future.done():
                    future.cancel()
            self._pending_requests.clear()

        self._state = CodexConnectionState.DISCONNECTED
        logger.debug("Codex app-server stopped")

    def add_notification_handler(self, method: str, handler: NotificationHandler) -> None:
        """
        Register a handler for a specific notification method.

        Args:
            method: Notification method name (e.g., "turn/started", "item/completed")
            handler: Callback function(method, params)
        """
        if method not in self._notification_handlers:
            self._notification_handlers[method] = []
        self._notification_handlers[method].append(handler)

    def remove_notification_handler(self, method: str, handler: NotificationHandler) -> None:
        """Remove a notification handler."""
        if method in self._notification_handlers:
            self._notification_handlers[method] = [
                h for h in self._notification_handlers[method] if h != handler
            ]

    # ===== Thread Management =====

    async def start_thread(
        self,
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
    ) -> CodexThread:
        """
        Start a new Codex conversation thread.

        Args:
            cwd: Working directory for the session
            model: Model override (e.g., "gpt-5.1-codex")
            approval_policy: Approval policy ("never", "unlessTrusted", etc.)
            sandbox: Sandbox mode ("workspaceWrite", "readOnly", etc.)

        Returns:
            CodexThread object with thread ID
        """
        params: dict[str, Any] = {}
        if cwd:
            params["cwd"] = cwd
        if model:
            params["model"] = model
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if sandbox:
            params["sandbox"] = sandbox

        result = await self._send_request("thread/start", params)

        thread_data = result.get("thread", {})
        thread = CodexThread(
            id=thread_data.get("id", ""),
            preview=thread_data.get("preview", ""),
            model_provider=thread_data.get("modelProvider", "openai"),
            created_at=thread_data.get("createdAt", 0),
        )

        self._threads[thread.id] = thread
        logger.debug(f"Started Codex thread: {thread.id}")
        return thread

    async def resume_thread(self, thread_id: str) -> CodexThread:
        """
        Resume an existing Codex conversation thread.

        Args:
            thread_id: ID of the thread to resume

        Returns:
            CodexThread object
        """
        result = await self._send_request("thread/resume", {"threadId": thread_id})

        thread_data = result.get("thread", {})
        thread = CodexThread(
            id=thread_data.get("id", thread_id),
            preview=thread_data.get("preview", ""),
            model_provider=thread_data.get("modelProvider", "openai"),
            created_at=thread_data.get("createdAt", 0),
        )

        self._threads[thread.id] = thread
        logger.debug(f"Resumed Codex thread: {thread.id}")
        return thread

    async def list_threads(
        self, cursor: str | None = None, limit: int = 25
    ) -> tuple[list[CodexThread], str | None]:
        """
        List stored Codex threads with pagination.

        Args:
            cursor: Pagination cursor from previous call
            limit: Maximum threads to return

        Returns:
            Tuple of (threads list, next_cursor or None)
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor

        result = await self._send_request("thread/list", params)

        threads = []
        for item in result.get("data", []):
            threads.append(
                CodexThread(
                    id=item.get("id", ""),
                    preview=item.get("preview", ""),
                    model_provider=item.get("modelProvider", "openai"),
                    created_at=item.get("createdAt", 0),
                )
            )

        next_cursor = result.get("nextCursor")
        return threads, next_cursor

    async def archive_thread(self, thread_id: str) -> None:
        """
        Archive a Codex thread.

        Args:
            thread_id: ID of the thread to archive
        """
        await self._send_request("thread/archive", {"threadId": thread_id})
        self._threads.pop(thread_id, None)
        logger.debug(f"Archived Codex thread: {thread_id}")

    # ===== Turn Management =====

    async def start_turn(
        self,
        thread_id: str,
        prompt: str,
        images: list[str] | None = None,
        **config_overrides: Any,
    ) -> CodexTurn:
        """
        Start a new turn (send user input and trigger generation).

        Args:
            thread_id: Thread ID to add turn to
            prompt: User's input text
            images: Optional list of image paths or URLs
            **config_overrides: Optional config overrides (cwd, model, etc.)

        Returns:
            CodexTurn object (initial state, updates via notifications)
        """
        # Build input array
        inputs: list[dict[str, Any]] = [{"type": "text", "text": prompt}]

        if images:
            for img in images:
                if img.startswith(("http://", "https://")):
                    inputs.append({"type": "image", "url": img})
                else:
                    inputs.append({"type": "localImage", "path": img})

        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": inputs,
        }
        params.update(config_overrides)

        result = await self._send_request("turn/start", params)

        turn_data = result.get("turn", {})
        turn = CodexTurn(
            id=turn_data.get("id", ""),
            thread_id=thread_id,
            status=turn_data.get("status", "inProgress"),
            items=turn_data.get("items", []),
            error=turn_data.get("error"),
        )

        logger.debug(f"Started turn {turn.id} in thread {thread_id}")
        return turn

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        """
        Interrupt an in-progress turn.

        Args:
            thread_id: Thread ID containing the turn
            turn_id: Turn ID to interrupt
        """
        await self._send_request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        logger.debug(f"Interrupted turn {turn_id}")

    async def run_turn(
        self,
        thread_id: str,
        prompt: str,
        images: list[str] | None = None,
        **config_overrides: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Run a turn and yield streaming events.

        This is the primary method for interacting with Codex. It starts a turn
        and yields all events until completion.

        Args:
            thread_id: Thread ID
            prompt: User's input text
            images: Optional image paths/URLs
            **config_overrides: Config overrides

        Yields:
            Event dicts with "type" and event-specific data

        Example:
            async for event in client.run_turn(thread.id, "Help me refactor"):
                if event["type"] == "item.completed":
                    print(event["item"]["text"])
        """
        # Queue to receive notifications
        event_queue: asyncio.Queue = asyncio.Queue()
        turn_completed = asyncio.Event()

        def on_event(method: str, params: dict[str, Any]) -> None:
            event_queue.put_nowait({"type": method, **params})
            if method == "turn/completed":
                turn_completed.set()

        # Register handlers for all turn-related events
        event_methods = [
            "turn/started",
            "turn/completed",
            "item/started",
            "item/completed",
            "item/agentMessage/delta",
        ]

        for method in event_methods:
            self.add_notification_handler(method, on_event)

        try:
            # Start the turn
            turn = await self.start_turn(thread_id, prompt, images=images, **config_overrides)

            yield {"type": "turn/created", "turn": turn.__dict__}

            # Yield events until turn completes
            while not turn_completed.is_set():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    yield event
                except TimeoutError:
                    continue

            # Drain remaining events
            while not event_queue.empty():
                yield event_queue.get_nowait()

        finally:
            # Unregister handlers
            for method in event_methods:
                self.remove_notification_handler(method, on_event)

    # ===== Authentication =====

    async def login_with_api_key(self, api_key: str) -> dict[str, Any]:
        """
        Authenticate using an OpenAI API key.

        Args:
            api_key: OpenAI API key (sk-...)

        Returns:
            Login result dict
        """
        result = await self._send_request(
            "account/login/start", {"type": "apiKey", "apiKey": api_key}
        )
        logger.debug("Logged in with API key")
        return result

    async def get_account_status(self) -> dict[str, Any]:
        """Get current account/authentication status."""
        return await self._send_request("account/status", {})

    # ===== Internal Methods =====

    def _next_request_id(self) -> int:
        """Generate unique request ID."""
        with self._request_id_lock:
            self._request_id += 1
            return self._request_id

    async def _send_request(
        self, method: str, params: dict[str, Any], timeout: float = 60.0
    ) -> dict[str, Any]:
        """
        Send a JSON-RPC request and wait for response.

        Args:
            method: RPC method name
            params: Method parameters
            timeout: Response timeout in seconds

        Returns:
            Result dict from response

        Raises:
            RuntimeError: If not connected or request fails
            TimeoutError: If response times out
        """
        if not self._process or not self._process.stdin:
            raise RuntimeError("Not connected to Codex app-server")

        request_id = self._next_request_id()
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "id": request_id,
            "params": params,
        }

        # Create future for response
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()

        with self._pending_requests_lock:
            self._pending_requests[request_id] = future

        try:
            # Send request
            request_line = json.dumps(request) + "\n"
            self._process.stdin.write(request_line)
            self._process.stdin.flush()

            logger.debug(f"Sent request: {method} (id={request_id})")

            # Wait for response
            result = await asyncio.wait_for(future, timeout=timeout)
            return cast(dict[str, Any], result)

        except TimeoutError:
            logger.error(f"Request {method} (id={request_id}) timed out")
            raise
        finally:
            with self._pending_requests_lock:
                self._pending_requests.pop(request_id, None)

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("Not connected to Codex app-server")

        notification = {"jsonrpc": "2.0", "method": method, "params": params}

        notification_line = json.dumps(notification) + "\n"
        self._process.stdin.write(notification_line)
        self._process.stdin.flush()

        logger.debug(f"Sent notification: {method}")

    async def _read_loop(self) -> None:
        """Background task to read responses and notifications."""
        if not self._process or not self._process.stdout:
            return

        loop = asyncio.get_event_loop()

        while not self._shutdown_event.is_set():
            try:
                # Read line in thread pool to avoid blocking
                line = await loop.run_in_executor(None, self._process.stdout.readline)

                if not line:
                    if self._process.poll() is not None:
                        logger.warning("Codex app-server process terminated")
                        self._state = CodexConnectionState.ERROR
                        break
                    continue

                # Parse JSON-RPC message
                try:
                    message = json.loads(line.strip())
                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid JSON from app-server: {e}")
                    continue

                # Handle response (has "id")
                if "id" in message:
                    request_id = message["id"]
                    with self._pending_requests_lock:
                        future = self._pending_requests.get(request_id)

                    if future and not future.done():
                        if "error" in message:
                            error = message["error"]
                            future.set_exception(
                                RuntimeError(
                                    f"RPC error {error.get('code')}: {error.get('message')}"
                                )
                            )
                        else:
                            future.set_result(message.get("result", {}))

                # Handle notification (no "id")
                elif "method" in message:
                    method = message["method"]
                    params = message.get("params", {})

                    logger.debug(f"Received notification: {method}")

                    # Call global handler
                    if self._on_notification:
                        try:
                            self._on_notification(method, params)
                        except Exception as e:
                            logger.error(f"Notification handler error: {e}")

                    # Call method-specific handlers
                    handlers = self._notification_handlers.get(method, [])
                    for handler in handlers:
                        try:
                            handler(method, params)
                        except Exception as e:
                            logger.error(f"Handler error for {method}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in read loop: {e}", exc_info=True)
                if self._shutdown_event.is_set():
                    break


# =============================================================================
# Shared Utilities
# =============================================================================


def _get_machine_id() -> str:
    """Get or generate a stable machine identifier based on hostname."""
    node = platform.node()
    if node:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, node))
    return str(uuid.uuid4())


def is_codex_available() -> bool:
    """Check if Codex CLI is installed and available.

    Returns:
        True if `codex` command is found in PATH.
    """
    return shutil.which("codex") is not None


# =============================================================================
# App-Server Adapter (for programmatic control)
# =============================================================================


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
        """Get or generate a machine identifier."""
        if self._machine_id is None:
            self._machine_id = _get_machine_id()
        return self._machine_id

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


# =============================================================================
# Notify Adapter (for installed hooks via `gobby install --codex`)
# =============================================================================


class CodexNotifyAdapter(BaseAdapter):  # type: ignore[misc]
    """Adapter for Codex CLI notify events.

    Translates notify payloads to unified HookEvent format.
    The notify hook only fires on `agent-turn-complete`, so we:
    - Treat first event for a thread as session start + prompt submit
    - Track thread IDs to avoid duplicate session registration

    This adapter handles events from the hook_dispatcher.py script installed
    by `gobby install --codex`.
    """

    source = SessionSource.CODEX

    # Track threads we've seen to avoid re-registering
    _seen_threads: set[str] = set()

    def __init__(self, hook_manager: "HookManager | None" = None):
        """Initialize the adapter.

        Args:
            hook_manager: Optional HookManager reference.
        """
        self._hook_manager = hook_manager
        self._machine_id: str | None = None

    def _get_machine_id(self) -> str:
        """Get or generate a machine identifier."""
        if self._machine_id is None:
            self._machine_id = _get_machine_id()
        return self._machine_id

    def _find_jsonl_path(self, thread_id: str) -> str | None:
        """Find the Codex session JSONL file for a thread.

        Codex stores sessions at: ~/.codex/sessions/YYYY/MM/DD/rollout-{timestamp}-{thread-id}.jsonl

        Args:
            thread_id: The Codex thread ID

        Returns:
            Path to the JSONL file, or None if not found
        """
        if not CODEX_SESSIONS_DIR.exists():
            return None

        # Search for file ending with thread-id.jsonl
        pattern = str(CODEX_SESSIONS_DIR / "**" / f"*{thread_id}.jsonl")
        matches = glob_module.glob(pattern, recursive=True)

        if matches:
            # Return the most recent match (in case of duplicates)
            return max(matches, key=os.path.getmtime)
        return None

    def _get_first_prompt(self, input_messages: list) -> str | None:
        """Extract the first user prompt from input_messages.

        Args:
            input_messages: List of user messages from Codex

        Returns:
            First prompt string, or None
        """
        if input_messages and isinstance(input_messages, list) and len(input_messages) > 0:
            first = input_messages[0]
            if isinstance(first, str):
                return first
            elif isinstance(first, dict):
                return first.get("text") or first.get("content")
        return None

    def translate_to_hook_event(self, native_event: dict) -> HookEvent | None:
        """Convert Codex notify payload to HookEvent.

        The native_event structure from /hooks/execute:
        {
            "hook_type": "AgentTurnComplete",
            "input_data": {
                "session_id": "thread-id",
                "event_type": "agent-turn-complete",
                "last_message": "...",
                "input_messages": [...],
                "cwd": "/path/to/project",
                "turn_id": "1"
            },
            "source": "codex"
        }

        Args:
            native_event: The payload from the HTTP endpoint.

        Returns:
            HookEvent for processing, or None if unsupported.
        """
        input_data = native_event.get("input_data", {})
        thread_id = input_data.get("session_id", "")
        event_type = input_data.get("event_type", "unknown")
        input_messages = input_data.get("input_messages", [])
        cwd = input_data.get("cwd") or os.getcwd()

        if not thread_id:
            logger.warning("Codex notify event missing thread_id")
            return None

        # Find the JSONL transcript file
        jsonl_path = self._find_jsonl_path(thread_id)

        # Track if this is the first event for this thread (for title synthesis)
        is_first_event = thread_id not in self._seen_threads
        if is_first_event:
            self._seen_threads.add(thread_id)

        # Get first prompt for title synthesis (only on first event)
        first_prompt = self._get_first_prompt(input_messages) if is_first_event else None

        # All Codex notify events are AFTER_AGENT (turn complete)
        # The HookManager will auto-register the session if it doesn't exist
        return HookEvent(
            event_type=HookEventType.AFTER_AGENT,
            session_id=thread_id,
            source=self.source,
            timestamp=datetime.now(),
            machine_id=self._get_machine_id(),
            data={
                "cwd": cwd,
                "event_type": event_type,
                "last_message": input_data.get("last_message", ""),
                "input_messages": input_messages,
                "transcript_path": jsonl_path,
                "is_first_event": is_first_event,
                "prompt": first_prompt,  # For title synthesis on first event
            },
        )

    def translate_from_hook_response(
        self, response: HookResponse, hook_type: str | None = None
    ) -> dict:
        """Convert HookResponse to Codex-expected format.

        Codex notify doesn't expect a response - it's fire-and-forget.
        This just returns a simple status dict for logging.

        Args:
            response: The HookResponse from HookManager.
            hook_type: Ignored (notify doesn't need response routing).

        Returns:
            Simple status dict.
        """
        return {
            "status": "processed",
            "decision": response.decision,
        }

    def handle_native(
        self, native_event: dict, hook_manager: "HookManager | None" = None
    ) -> dict:
        """Process native Codex notify event.

        Args:
            native_event: The payload from HTTP endpoint.
            hook_manager: Optional HookManager (uses instance if not provided).

        Returns:
            Response dict.
        """
        manager = hook_manager or self._hook_manager
        if not manager:
            logger.warning("No HookManager available for Codex notify event")
            return {"status": "error", "message": "No HookManager"}

        hook_event = self.translate_to_hook_event(native_event)
        if not hook_event:
            return {"status": "skipped", "message": "Unsupported event"}

        hook_response = manager.handle(hook_event)
        return self.translate_from_hook_response(hook_response)

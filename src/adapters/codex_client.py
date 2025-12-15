"""
Codex App Server Client.

Manages the `codex app-server` subprocess and provides programmatic control
over Codex sessions via JSON-RPC 2.0 protocol.

Architecture:
    gobby-daemon
    └── CodexAppServerClient
        ├── Spawns: `codex app-server` (stdio subprocess)
        ├── Protocol: JSON-RPC 2.0 over stdin/stdout
        └── Features:
            - Thread management (start, resume, list, archive)
            - Turn management (start, interrupt)
            - Streaming notifications
            - Session tracking integration
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast

logger = logging.getLogger(__name__)


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

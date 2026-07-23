"""
CodexAppServerClient public facade.

This module keeps the stable CodexAppServerClient import path while delegating
implementation details to focused helper modules.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess  # nosec B404 # subprocess needed for Codex app-server process
import threading
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from gobby.adapters.codex_impl import (
    client_api,
    client_lifecycle,
    client_notifications,
    client_rpc,
)
from gobby.adapters.codex_impl.types import (
    CodexConnectionState,
    CodexThread,
    CodexTurn,
    NotificationHandler,
)
from gobby.adapters.subprocess_stderr import SubprocessStderrDrain

logger = logging.getLogger(__name__)

# Codex session storage location
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


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
        config_overrides: tuple[str, ...] | list[str] | None = None,
        enabled_features: tuple[str, ...] | list[str] | None = None,
        disabled_features: tuple[str, ...] | list[str] | None = None,
        global_args: tuple[str, ...] | list[str] | None = None,
        env_overrides: Mapping[str, str] | None = None,
    ) -> None:
        """
        Initialize the Codex app-server client.

        Args:
            codex_command: Path to the codex binary (default: "codex")
            on_notification: Optional callback for all notifications
            config_overrides: Optional `-c key=value` overrides for app-server startup
            enabled_features: Optional feature names to pass with `--enable`
            disabled_features: Optional feature names to pass with `--disable`
            global_args: Optional Codex arguments that must precede the app-server subcommand
        """
        self._codex_command = codex_command
        self._on_notification = on_notification
        self._config_overrides = tuple(config_overrides or ())
        self._enabled_features = tuple(enabled_features or ())
        self._disabled_features = tuple(disabled_features or ())
        self._global_args = tuple(global_args or ())
        self._env_overrides = dict(env_overrides or {})
        self._redacted_env_values = tuple(
            value
            for key, value in self._env_overrides.items()
            if value and any(marker in key.upper() for marker in ("KEY", "TOKEN", "SECRET"))
        )

        self._process: subprocess.Popen[str] | None = None
        self._state = CodexConnectionState.DISCONNECTED
        self._request_id = 0
        self._request_id_lock = threading.Lock()

        # Pending requests waiting for responses
        self._pending_requests: dict[int, asyncio.Future[Any]] = {}
        self._pending_requests_lock = threading.Lock()

        # Notification handlers by method
        self._notification_handlers: dict[str, list[NotificationHandler]] = {}

        # Approval handler for incoming requests (bidirectional blocking)
        self._approval_handler: Any | None = None

        # Reader task
        self._reader_task: asyncio.Task[None] | None = None
        self._incoming_request_tasks: set[asyncio.Task[None]] = set()
        self._stderr_drain = SubprocessStderrDrain("Codex app-server", logger=logger)
        self._shutdown_event = asyncio.Event()

        # Thread tracking for session management
        self._threads: dict[str, CodexThread] = {}
        self._thread_cwds: dict[str, str] = {}
        self._thread_terminal_contexts: dict[str, dict[str, Any]] = {}
        self._pending_thread_terminal_contexts: list[dict[str, Any]] = []
        self._pending_turn_prompts_by_thread: dict[str, str] = {}
        self._turn_prompts: dict[str, str] = {}

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
        await client_lifecycle.start(self, subprocess)

    async def stop(self) -> None:
        await client_lifecycle.stop(self)

    def add_notification_handler(self, method: str, handler: NotificationHandler) -> None:
        client_notifications.add_notification_handler(self, method, handler)

    def remove_notification_handler(self, method: str, handler: NotificationHandler) -> None:
        client_notifications.remove_notification_handler(self, method, handler)

    def register_approval_handler(self, handler: Any | None) -> None:
        client_notifications.register_approval_handler(self, handler)

    async def start_thread(
        self,
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        terminal_context: dict[str, Any] | None = None,
        ephemeral: bool = False,
    ) -> CodexThread:
        return await client_api.start_thread(
            self,
            cwd=cwd,
            model=model,
            approval_policy=approval_policy,
            sandbox=sandbox,
            terminal_context=terminal_context,
            ephemeral=ephemeral,
        )

    async def resume_thread(self, thread_id: str) -> CodexThread:
        return await client_api.resume_thread(self, thread_id)

    async def list_threads(
        self, cursor: str | None = None, limit: int = 25
    ) -> tuple[list[CodexThread], str | None]:
        return await client_api.list_threads(self, cursor=cursor, limit=limit)

    async def archive_thread(self, thread_id: str) -> None:
        await client_api.archive_thread(self, thread_id)

    async def list_models(
        self,
        *,
        limit: int = 100,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        return await client_api.list_models(self, limit=limit, include_hidden=include_hidden)

    async def start_turn(
        self,
        thread_id: str,
        prompt: str,
        images: list[str] | None = None,
        context_prefix: str | None = None,
        effort: str | None = None,
        **config_overrides: Any,
    ) -> CodexTurn:
        return await client_api.start_turn(
            self,
            thread_id,
            prompt,
            images=images,
            context_prefix=context_prefix,
            effort=effort,
            **config_overrides,
        )

    def _enrich_notification(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return client_notifications.enrich_notification(self, method, params)

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        await client_api.interrupt_turn(self, thread_id, turn_id)

    async def run_turn(
        self,
        thread_id: str,
        prompt: str,
        images: list[str] | None = None,
        **config_overrides: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in client_api.run_turn(
            self,
            thread_id,
            prompt,
            images=images,
            **config_overrides,
        ):
            yield event

    @staticmethod
    def _notification_thread_id(params: dict[str, Any]) -> str | None:
        return client_notifications.notification_thread_id(params)

    async def login_with_api_key(self, api_key: str) -> dict[str, Any]:
        return await client_api.login_with_api_key(self, api_key)

    async def get_account_status(self) -> dict[str, Any]:
        return await client_api.get_account_status(self)

    def _next_request_id(self) -> int:
        return client_rpc.next_request_id(self)

    async def _send_request(
        self, method: str, params: dict[str, Any], timeout: float = 60.0
    ) -> dict[str, Any]:
        return await client_rpc.send_request(self, method, params, timeout=timeout)

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        await client_rpc.send_notification(self, method, params)

    async def _handle_incoming_request(self, message: dict[str, Any]) -> None:
        await client_rpc.handle_incoming_request(self, message)

    def _dispatch_incoming_request(self, message: dict[str, Any]) -> None:
        client_rpc.dispatch_incoming_request(self, message)

    async def _send_stdin_response(self, response: dict[str, Any]) -> None:
        await client_rpc.send_stdin_response(self, response)

    async def _read_loop(self) -> None:
        await client_rpc.read_loop(self)


__all__ = [
    "CodexAppServerClient",
    "CODEX_SESSIONS_DIR",
]

"""Subprocess lifecycle helpers for CodexAppServerClient."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from gobby.adapters.codex_impl.types import CodexConnectionState

if TYPE_CHECKING:
    from gobby.adapters.codex_impl.client import CodexAppServerClient

logger = logging.getLogger(__name__)


async def start(client: CodexAppServerClient, subprocess_module: Any) -> None:
    """
    Start the Codex app-server subprocess and initialize connection.

    Raises:
        RuntimeError: If already connected or failed to start
    """
    if client._state == CodexConnectionState.CONNECTED:
        logger.warning("CodexAppServerClient already connected")
        return

    client._state = CodexConnectionState.CONNECTING
    logger.debug("Starting Codex app-server...")

    try:
        env = os.environ.copy()
        # Prevent installed Codex hooks from registering nested daemon sessions.
        env["GOBBY_HOOKS_DISABLED"] = "1"
        command = [client._codex_command, "app-server"]
        for override in client._config_overrides:
            command.extend(["-c", override])
        for feature in client._enabled_features:
            command.extend(["--enable", feature])
        for feature in client._disabled_features:
            command.extend(["--disable", feature])

        # Start the subprocess
        client._process = subprocess_module.Popen(  # nosec B603
            command,
            stdin=subprocess_module.PIPE,
            stdout=subprocess_module.PIPE,
            stderr=subprocess_module.PIPE,
            text=True,
            bufsize=1,  # Line buffered
            env=env,
        )

        # Start the reader task
        client._shutdown_event.clear()
        client._reader_task = asyncio.create_task(client._read_loop())
        client._stderr_drain.start_text(client._process.stderr)

        # Send initialize request
        result = await client._send_request(
            "initialize",
            {
                "clientInfo": {
                    "name": client.CLIENT_NAME,
                    "title": client.CLIENT_TITLE,
                    "version": client.CLIENT_VERSION,
                }
            },
        )

        user_agent = result.get("userAgent", "unknown")
        logger.debug(f"Codex app-server initialized: {user_agent}")

        # Send initialized notification
        await client._send_notification("initialized", {})

        client._state = CodexConnectionState.CONNECTED
        logger.debug("Codex app-server connection established")

    except Exception as e:
        client._state = CodexConnectionState.ERROR
        logger.error(f"Failed to start Codex app-server: {e}", exc_info=True)
        await stop(client)
        raise RuntimeError(f"Failed to start Codex app-server: {e}") from e


async def stop(client: CodexAppServerClient) -> None:
    """Stop the Codex app-server subprocess."""
    logger.debug("Stopping Codex app-server...")

    client._shutdown_event.set()

    # Cancel reader task
    if client._reader_task and not client._reader_task.done():
        client._reader_task.cancel()
        try:
            await client._reader_task
        except asyncio.CancelledError:
            pass

    # Cancel request handlers spawned by the reader loop
    if client._incoming_request_tasks:
        tasks = tuple(client._incoming_request_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        client._incoming_request_tasks.clear()

    # Terminate process
    if client._process:
        try:
            if client._process.stdin:
                client._process.stdin.close()
            client._process.terminate()
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(loop.run_in_executor(None, client._process.wait), timeout=5.0)
        except Exception as e:
            logger.warning(f"Error terminating Codex app-server: {e}")
            client._process.kill()
        finally:
            client._process = None
    await client._stderr_drain.stop()

    # Cancel pending requests
    with client._pending_requests_lock:
        for future in client._pending_requests.values():
            if not future.done():
                future.cancel()
        client._pending_requests.clear()

    client._state = CodexConnectionState.DISCONNECTED
    logger.debug("Codex app-server stopped")

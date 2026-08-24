"""Subprocess lifecycle helpers for CodexAppServerClient."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from gobby.adapters.codex_impl.types import CodexConnectionState
from gobby.utils.stream_pump import open_stream_pump_executor

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
        env.update(client._env_overrides)
        # Prevent installed Codex hooks from registering nested daemon sessions.
        env["GOBBY_HOOKS_DISABLED"] = "1"
        command = [client._codex_command, *client._global_args, "app-server"]
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
        initialize_params: dict[str, Any] = {
            "clientInfo": {
                "name": client.CLIENT_NAME,
                "title": client.CLIENT_TITLE,
                "version": client.CLIENT_VERSION,
            }
        }
        if client._experimental_api:
            initialize_params["capabilities"] = {"experimentalApi": True}
        result = await client._send_request("initialize", initialize_params)

        user_agent = result.get("userAgent", "unknown")
        logger.debug("Codex app-server initialized: %s", user_agent)

        # Send initialized notification
        await client._send_notification("initialized", {})

        client._state = CodexConnectionState.CONNECTED
        logger.debug("Codex app-server connection established")

    except Exception as e:
        client._state = CodexConnectionState.ERROR
        await client._stderr_drain.wait_finished()
        stderr = client._stderr_drain.compact_text()
        failure_detail = f"{e}; stderr: {stderr}" if stderr else str(e)
        for secret in client._redacted_env_values:
            if secret:
                failure_detail = failure_detail.replace(secret, "[REDACTED]")
        logger.debug(
            "Failed to start Codex app-server: %s",
            failure_detail,
            exc_info=True,
        )
        await stop(client)
        raise RuntimeError(f"Failed to start Codex app-server: {failure_detail}") from e


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

    # Terminate process and release every pipe owned by Popen.
    process = client._process
    try:
        if process:
            try:
                process.terminate()
                loop = asyncio.get_running_loop()
                # A child that ignores SIGTERM holds this wait for the full
                # five seconds, and every failed start() runs it too. Keep it
                # off the shared default executor (#20839).
                reaper = open_stream_pump_executor("codex-wait")
                try:
                    await asyncio.wait_for(loop.run_in_executor(reaper, process.wait), timeout=5.0)
                finally:
                    reaper.shutdown(wait=False)
            except Exception as e:
                logger.warning("Error terminating Codex app-server: %s", e)
                process.kill()
    finally:
        await client._stderr_drain.stop()
        if process:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
            client._process = None

    # Cancel pending requests
    with client._pending_requests_lock:
        for future in client._pending_requests.values():
            if not future.done():
                future.cancel()
        client._pending_requests.clear()

    client._state = CodexConnectionState.DISCONNECTED
    logger.debug("Codex app-server stopped")

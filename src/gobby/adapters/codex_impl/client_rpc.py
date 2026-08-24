"""JSON-RPC helpers for CodexAppServerClient."""

from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, cast

from gobby.adapters.codex_impl.types import CodexConnectionState
from gobby.utils.stream_pump import open_stream_pump_executor

if TYPE_CHECKING:
    from gobby.adapters.codex_impl.client import CodexAppServerClient

logger = logging.getLogger(__name__)


def next_request_id(client: CodexAppServerClient) -> int:
    """Generate unique request ID."""
    with client._request_id_lock:
        client._request_id += 1
        return client._request_id


async def send_request(
    client: CodexAppServerClient,
    method: str,
    params: dict[str, Any],
    timeout: float = 60.0,
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
    if not client._process or not client._process.stdin:
        raise RuntimeError("Not connected to Codex app-server")
    if client._state is CodexConnectionState.ERROR:
        raise ConnectionError("Codex app-server process is unavailable")

    request_id = client._next_request_id()
    request = {
        "jsonrpc": "2.0",
        "method": method,
        "id": request_id,
        "params": params,
    }

    # Create future for response
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    with client._pending_requests_lock:
        client._pending_requests[request_id] = future

    try:
        # Send request - offload blocking I/O to thread executor
        request_line = json.dumps(request) + "\n"

        # Capture local references to avoid race with stop()
        process = client._process
        stdin = process.stdin if process is not None else None

        def write_request() -> None:
            if stdin is None:
                return
            stdin.write(request_line)
            stdin.flush()

        if stdin is None:
            raise RuntimeError("Not connected to Codex app-server")

        await loop.run_in_executor(None, write_request)

        logger.debug("Sent request: %s (id=%s)", method, request_id)

        # Wait for response
        result = await asyncio.wait_for(future, timeout=timeout)
        return cast(dict[str, Any], result)

    except TimeoutError:
        logger.error("Request %s (id=%s) timed out", method, request_id)
        raise
    finally:
        with client._pending_requests_lock:
            client._pending_requests.pop(request_id, None)


async def send_notification(
    client: CodexAppServerClient,
    method: str,
    params: dict[str, Any],
) -> None:
    """Send a JSON-RPC notification (no response expected)."""
    if not client._process or not client._process.stdin:
        raise RuntimeError("Not connected to Codex app-server")

    notification = {"jsonrpc": "2.0", "method": method, "params": params}

    notification_line = json.dumps(notification) + "\n"

    # Capture local references to avoid race with stop()
    process = client._process
    stdin = process.stdin if process is not None else None

    def write_notification() -> None:
        if stdin is None:
            return
        stdin.write(notification_line)
        stdin.flush()

    if stdin is None:
        raise RuntimeError("Not connected to Codex app-server")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, write_notification)

    logger.debug("Sent notification: %s", method)


async def handle_incoming_request(client: CodexAppServerClient, message: dict[str, Any]) -> None:
    """Handle an incoming JSON-RPC request from Codex (e.g., approval requests).

    Routes the request to the registered approval handler and sends back
    a JSON-RPC response with the handler's decision.

    Args:
        message: JSON-RPC request with id, method, and params.
    """
    request_id = message["id"]
    method = message["method"]
    params = message.get("params", {})

    handler = client._request_handlers.get(method)
    if handler is None:
        handler = client._approval_handler
    if handler is None:
        logger.debug("No handler for incoming request: %s", method)
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"No handler registered for {method}"},
        }
        await client._send_stdin_response(response)
        return

    logger.debug("Handling incoming request: %s (id=%s)", method, request_id)

    try:
        if method in client._request_handlers:
            result = await handler(params)
        else:
            result = await handler(method, params)
        response = {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as e:
        logger.error("Incoming request handler error for %s: %s", method, e)
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32603, "message": str(e)},
        }

    await client._send_stdin_response(response)


async def send_stdin_response(client: CodexAppServerClient, response: dict[str, Any]) -> None:
    """Send a JSON-RPC response to the Codex process via stdin.

    Uses run_in_executor since proc.stdin is a synchronous pipe (subprocess.Popen).
    """
    proc = client._process
    if proc and proc.stdin:
        response_line = json.dumps(response) + "\n"

        def write_response() -> None:
            if proc and proc.stdin:
                try:
                    proc.stdin.write(response_line)
                    proc.stdin.flush()
                except OSError:
                    pass

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, write_response)


def dispatch_incoming_request(client: CodexAppServerClient, message: dict[str, Any]) -> None:
    task = asyncio.create_task(client._handle_incoming_request(message))
    client._incoming_request_tasks.add(task)

    def discard_task(done_task: asyncio.Task[None]) -> None:
        client._incoming_request_tasks.discard(done_task)
        if done_task.cancelled():
            return
        if exc := done_task.exception():
            logger.error("Incoming Codex request handler failed", exc_info=exc)

    task.add_done_callback(discard_task)


async def read_loop(client: CodexAppServerClient) -> None:
    """Background task to read responses and notifications."""
    if not client._process or not client._process.stdout:
        return

    # readline blocks until the app-server speaks, which for a quiet client is
    # the whole life of the process. Park that on a thread this loop owns
    # rather than on the shared default executor (#20839).
    executor = open_stream_pump_executor("codex-stdout")
    try:
        await _consume_stdout(client, executor)
    finally:
        executor.shutdown(wait=False)


async def _consume_stdout(client: CodexAppServerClient, executor: ThreadPoolExecutor) -> None:
    """Dispatch every JSON-RPC line the app-server writes until shutdown."""
    loop = asyncio.get_running_loop()

    while not client._shutdown_event.is_set():
        try:
            # Capture local references to avoid race with stop()
            proc = client._process
            if proc is None:
                break
            stdout = proc.stdout
            if stdout is None:
                break

            # Read line on the pump thread to avoid blocking the event loop
            line = await loop.run_in_executor(executor, stdout.readline)

            if not line:
                if client._shutdown_event.is_set():
                    break
                return_code = proc.poll()
                if return_code is not None:
                    logger.warning("Codex app-server process terminated with code %s", return_code)
                    client._state = CodexConnectionState.ERROR
                    with client._pending_requests_lock:
                        for pending_future in client._pending_requests.values():
                            if not pending_future.done():
                                pending_future.set_exception(
                                    ConnectionError("Codex app-server process terminated")
                                )
                        client._pending_requests.clear()
                    break
                continue

            try:
                message = json.loads(line.strip())
            except json.JSONDecodeError as e:
                logger.warning("Invalid JSON from app-server: %s", e)
                continue

            if "method" in message and "id" in message:
                # Codex uses an independent id space for inbound requests,
                # so these ids can collide with our outgoing request ids.
                client._dispatch_incoming_request(message)

            # Handle response to our outgoing request (has "id" without "method")
            elif "id" in message:
                request_id = message["id"]
                with client._pending_requests_lock:
                    future = client._pending_requests.get(request_id)

                if future and not future.done():
                    # Response to our outgoing request
                    if "error" in message:
                        error = message["error"]
                        future.set_exception(
                            RuntimeError(f"RPC error {error.get('code')}: {error.get('message')}")
                        )
                    else:
                        future.set_result(message.get("result", {}))

            elif "method" in message:
                method = message["method"]
                params = message.get("params", {})
                if isinstance(params, dict):
                    params = client._enrich_notification(method, params)

                logger.debug("Received notification: %s", method)

                if client._on_notification:
                    try:
                        client._on_notification(method, params)
                    except Exception as e:
                        logger.error("Notification handler error: %s", e)

                handlers = client._notification_handlers.get(method, [])
                for handler in handlers:
                    try:
                        handler(method, params)
                    except Exception as e:
                        logger.error("Handler error for %s: %s", method, e)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("Error in read loop: %s", e)
            if client._shutdown_event.is_set():
                break

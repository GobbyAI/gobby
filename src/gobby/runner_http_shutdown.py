"""Bounded Uvicorn request drain and task termination for daemon shutdown."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import uvicorn

logger = logging.getLogger(__name__)

_GOBBY_SHUTDOWN_DRAIN_MESSAGE = "Gobby shutdown drain"


async def begin_uvicorn_http_shutdown(
    server: uvicorn.Server,
    *,
    connection_grace_seconds: float,
    connection_drain_seconds: float,
    request_cancel_timeout_seconds: float,
) -> None:
    """Drain active HTTP work, then tell Uvicorn to exit."""
    try:
        await _drain_uvicorn_http_connections(
            server,
            connection_grace_seconds=connection_grace_seconds,
            connection_drain_seconds=connection_drain_seconds,
            request_cancel_timeout_seconds=request_cancel_timeout_seconds,
        )
    finally:
        server.should_exit = True


async def settle_uvicorn_http_server(
    server_task: asyncio.Task[Any],
    *,
    timeout_seconds: float,
) -> None:
    """Settle Uvicorn within a bound, cancelling its task on timeout."""
    if server_task.done():
        _consume_task_result(server_task)
        return
    _done, pending = await asyncio.wait({server_task}, timeout=timeout_seconds)
    if not pending:
        _consume_task_result(server_task)
        return
    logger.warning("HTTP server did not settle before shutdown timeout; cancelling task")
    server_task.cancel(_GOBBY_SHUTDOWN_DRAIN_MESSAGE)
    _done, pending = await asyncio.wait({server_task}, timeout=2.0)
    if pending:
        server_task.add_done_callback(_consume_task_result)
    else:
        _consume_task_result(server_task)


async def force_terminate_uvicorn_http_server(
    server: uvicorn.Server,
    server_task: asyncio.Task[Any],
    *,
    request_cancel_timeout_seconds: float,
) -> None:
    """Cancel request and Uvicorn tasks after a graceful path degrades."""
    server.should_exit = True
    state = getattr(server, "server_state", None)
    await _cancel_remaining_request_tasks(
        getattr(state, "tasks", None),
        timeout_seconds=request_cancel_timeout_seconds,
    )
    if server_task.done():
        _consume_task_result(server_task)
        return
    server_task.cancel(_GOBBY_SHUTDOWN_DRAIN_MESSAGE)
    _done, pending = await asyncio.wait(
        {server_task},
        timeout=request_cancel_timeout_seconds,
    )
    if pending:
        server_task.add_done_callback(_consume_task_result)
    else:
        _consume_task_result(server_task)


async def force_terminate_uvicorn_http_server_under_cancellation(
    server: uvicorn.Server,
    server_task: asyncio.Task[Any],
    *,
    request_cancel_timeout_seconds: float,
) -> asyncio.CancelledError | None:
    """Defer caller cancellation until forced HTTP termination settles."""
    owned = asyncio.create_task(
        force_terminate_uvicorn_http_server(
            server,
            server_task,
            request_cancel_timeout_seconds=request_cancel_timeout_seconds,
        ),
        name="forced-http-server-termination",
    )
    cancellation: asyncio.CancelledError | None = None
    while not owned.done():
        try:
            await asyncio.shield(owned)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    _consume_task_result(owned)
    return cancellation


async def _drain_uvicorn_http_connections(
    server: uvicorn.Server,
    *,
    connection_grace_seconds: float,
    connection_drain_seconds: float,
    request_cancel_timeout_seconds: float,
) -> None:
    """Ask Uvicorn connections to close and cancel remaining request tasks."""
    state = getattr(server, "server_state", None)
    connections = getattr(state, "connections", None)
    tasks = getattr(state, "tasks", None)
    if connections is None and tasks is None:
        return

    if connections:
        for connection in list(connections):
            shutdown = getattr(connection, "shutdown", None)
            if callable(shutdown):
                shutdown()

    if await _wait_for_uvicorn_http_drain(
        connections,
        tasks,
        timeout=connection_grace_seconds,
    ):
        return

    if connections:
        for connection in list(connections):
            transport = getattr(connection, "transport", None)
            close = getattr(transport, "close", None)
            is_closing = getattr(transport, "is_closing", None)
            if callable(close) and not (callable(is_closing) and is_closing()):
                close()

    drained = await _wait_for_uvicorn_http_drain(
        connections,
        tasks,
        timeout=connection_drain_seconds,
    )
    if drained:
        return

    remaining_connections = len(connections or ())
    remaining_tasks = len(_live_uvicorn_http_tasks(tasks))
    if remaining_tasks:
        await _cancel_remaining_request_tasks(
            tasks,
            timeout_seconds=request_cancel_timeout_seconds,
        )
        remaining_connections = len(connections or ())
        remaining_tasks = len(_live_uvicorn_http_tasks(tasks))

    logger.debug(
        "HTTP request drain left %d connection(s) and %d live task(s) after cancellation",
        remaining_connections,
        remaining_tasks,
    )


async def _wait_for_uvicorn_http_drain(
    connections: Any,
    tasks: Any,
    *,
    timeout: float,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if not connections and not tasks:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.05)


async def _cancel_remaining_request_tasks(tasks: Any, *, timeout_seconds: float) -> None:
    task_list = _live_uvicorn_http_tasks(tasks)
    if not task_list:
        return
    for task in task_list:
        task.cancel(_GOBBY_SHUTDOWN_DRAIN_MESSAGE)

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while _live_uvicorn_http_tasks(tasks) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)


def _live_uvicorn_http_tasks(tasks: Any) -> list[asyncio.Task[Any]]:
    if not tasks:
        return []
    return [task for task in list(tasks) if isinstance(task, asyncio.Task) and not task.done()]


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.exception()
    except asyncio.CancelledError:
        pass

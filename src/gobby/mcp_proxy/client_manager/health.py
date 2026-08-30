"""Health monitoring helpers for MCP client manager."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping, MutableSet
from typing import Any, Protocol

from gobby.mcp_proxy.models import HealthState


class _HealthConnection(Protocol):
    @property
    def is_connected(self) -> bool: ...

    @property
    def last_health_error(self) -> str | None: ...

    def health_check(self, timeout: float = 5.0) -> Coroutine[Any, Any, bool]: ...


class _HealthStatus(Protocol):
    name: str
    health: HealthState
    consecutive_failures: int
    last_error: str | None

    def record_failure(self, error: str) -> None: ...

    def record_success(self) -> None: ...


class _HealthManager(Protocol):
    @property
    def _connections(self) -> Mapping[str, _HealthConnection]: ...

    @property
    def _health_check_interval(self) -> float: ...

    @property
    def _reconnect_tasks(self) -> MutableSet[asyncio.Task[None]]: ...

    @property
    def _running(self) -> bool: ...

    @property
    def health(self) -> Mapping[str, _HealthStatus]: ...

    def _reconnect(self, server_id: str) -> Coroutine[Any, Any, None]: ...


def _reconnect_done_callback(
    task: asyncio.Task[None],
    reconnect_tasks: MutableSet[asyncio.Task[None]],
    logger: logging.Logger,
) -> None:
    reconnect_tasks.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        logger.debug("Reconnect task was cancelled")
    except Exception:
        logger.exception("Reconnect task failed")


def _health_failure_reason(connection: _HealthConnection, result: Any) -> str:
    if isinstance(result, BaseException):
        message = " ".join(str(result).split())
        detail = f"{type(result).__name__}: {message}" if message else type(result).__name__
    else:
        connection_error = getattr(connection, "last_health_error", None)
        detail = connection_error if isinstance(connection_error, str) else ""

    normalized = " ".join(detail.split())
    return normalized[:500] if normalized else "Health check failed"


async def health_check_all(manager: _HealthManager) -> dict[str, Any]:
    """Perform an immediate health check on all connected transports."""
    tasks: list[Awaitable[Any]] = []
    server_ids: list[str] = []
    connections: list[_HealthConnection] = []

    for server_id, connection in manager._connections.items():
        if connection.is_connected:
            tasks.append(connection.health_check(timeout=5.0))
            server_ids.append(server_id)
            connections.append(connection)

    if not tasks:
        return {}

    results = await asyncio.gather(*tasks, return_exceptions=True)

    health_status: dict[str, bool] = {}
    for server_id, connection, result in zip(server_ids, connections, results, strict=True):
        if isinstance(result, BaseException) or result is False:
            reason = _health_failure_reason(connection, result)
            manager.health[server_id].record_failure(reason)
            health_status[server_id] = False
        else:
            manager.health[server_id].record_success()
            health_status[server_id] = True

    return health_status


async def monitor_health(
    manager: _HealthManager,
    logger: logging.Logger,
    sleep: Callable[[float], Awaitable[Any]],
) -> None:
    """Run the background connection health monitor loop."""
    while manager._running:
        try:
            await sleep(manager._health_check_interval)

            tasks: list[Awaitable[Any]] = []
            server_ids: list[str] = []
            connections: list[_HealthConnection] = []

            for server_id, connection in manager._connections.items():
                if connection.is_connected:
                    tasks.append(connection.health_check(timeout=5.0))
                    server_ids.append(server_id)
                    connections.append(connection)

            if not tasks:
                continue

            results = await asyncio.gather(*tasks, return_exceptions=True)
            configs = getattr(manager, "_configs", {})

            for server_id, connection, result in zip(server_ids, connections, results, strict=True):
                status = manager.health[server_id]
                config = configs.get(server_id)
                label = config.name if config is not None else status.name
                project_id = (
                    config.project_id if config is not None else getattr(status, "project_id", None)
                )
                if isinstance(result, BaseException) or result is False:
                    previous_health = status.health
                    reason = _health_failure_reason(connection, result)
                    status.record_failure(reason)
                    failure_context = {
                        "server_id": server_id,
                        "server_name": label,
                        "project_id": project_id,
                        "previous_health": previous_health.value,
                        "current_health": status.health.value,
                        "consecutive_failures": status.consecutive_failures,
                        "last_error": status.last_error,
                    }
                    if status.health == HealthState.UNHEALTHY:
                        if previous_health != HealthState.UNHEALTHY:
                            logger.warning(
                                "Health check failed for %s (%s); server is unhealthy",
                                label,
                                project_id,
                                extra=failure_context,
                            )
                        else:
                            logger.debug(
                                "Health check failed for %s",
                                label,
                                extra=failure_context,
                            )
                    else:
                        logger.debug(
                            "Health check failed for %s",
                            label,
                            extra=failure_context,
                        )

                    if status.health == HealthState.UNHEALTHY:
                        logger.info(
                            "Attempting reconnection for unhealthy server: %s (%s)",
                            label,
                            project_id,
                        )
                        task = asyncio.create_task(manager._reconnect(server_id))
                        manager._reconnect_tasks.add(task)
                        task.add_done_callback(
                            lambda done_task: _reconnect_done_callback(
                                done_task,
                                manager._reconnect_tasks,
                                logger,
                            )
                        )
                else:
                    status.record_success()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Error in health monitor: %s", exc)


def get_server_health(manager: Any) -> dict[str, dict[str, Any]]:
    """Format health status for all known servers, keyed by server id."""
    report: dict[str, dict[str, Any]] = {}
    configs = getattr(manager, "_configs", {})
    for server_id, status in manager.health.items():
        config = configs.get(server_id)
        entry: dict[str, Any] = {
            "state": status.state.value,
            "health": status.health.value,
            "name": status.name,
            "project_id": (
                config.project_id if config is not None else getattr(status, "project_id", None)
            ),
            "last_check": (
                status.last_health_check.isoformat() if status.last_health_check else None
            ),
            "failures": status.consecutive_failures,
            "last_error": status.last_error,
            "response_time_ms": status.response_time_ms,
        }
        missing = getattr(status, "missing_secrets", None)
        if missing:
            entry["missing_secrets"] = list(missing)
        report[server_id] = entry
    return report

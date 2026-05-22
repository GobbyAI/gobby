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

    def health_check(self, timeout: float = 5.0) -> Coroutine[Any, Any, bool]: ...


class _HealthStatus(Protocol):
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

    def _reconnect(self, server_name: str) -> Coroutine[Any, Any, None]: ...


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


async def health_check_all(manager: _HealthManager) -> dict[str, Any]:
    """Perform an immediate health check on all connected transports."""
    tasks: list[Awaitable[Any]] = []
    server_names: list[str] = []

    for name, connection in manager._connections.items():
        if connection.is_connected:
            tasks.append(connection.health_check(timeout=5.0))
            server_names.append(name)

    if not tasks:
        return {}

    results = await asyncio.gather(*tasks, return_exceptions=True)

    health_status: dict[str, bool] = {}
    for name, result in zip(server_names, results, strict=True):
        if isinstance(result, Exception) or result is False:
            manager.health[name].record_failure("Health check failed")
            health_status[name] = False
        else:
            manager.health[name].record_success()
            health_status[name] = True

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
            server_names: list[str] = []

            for name, connection in manager._connections.items():
                if connection.is_connected:
                    tasks.append(connection.health_check(timeout=5.0))
                    server_names.append(name)

            if not tasks:
                continue

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for name, result in zip(server_names, results, strict=True):
                if isinstance(result, Exception) or result is False:
                    previous_health = manager.health[name].health
                    manager.health[name].record_failure("Health check failed")
                    failure_context = {
                        "server_name": name,
                        "previous_health": previous_health.value,
                        "current_health": manager.health[name].health.value,
                        "consecutive_failures": manager.health[name].consecutive_failures,
                        "last_error": manager.health[name].last_error,
                    }
                    if manager.health[name].health == HealthState.UNHEALTHY:
                        if previous_health != HealthState.UNHEALTHY:
                            logger.warning(
                                "Health check failed for %s; server is unhealthy",
                                name,
                                extra=failure_context,
                            )
                        else:
                            logger.debug(
                                "Health check failed for %s",
                                name,
                                extra=failure_context,
                            )
                    else:
                        logger.debug(
                            "Health check failed for %s",
                            name,
                            extra=failure_context,
                        )

                    if manager.health[name].health == HealthState.UNHEALTHY:
                        logger.info("Attempting reconnection for unhealthy server: %s", name)
                        task = asyncio.create_task(manager._reconnect(name))
                        manager._reconnect_tasks.add(task)
                        task.add_done_callback(
                            lambda done_task: _reconnect_done_callback(
                                done_task,
                                manager._reconnect_tasks,
                                logger,
                            )
                        )
                else:
                    manager.health[name].record_success()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Error in health monitor: %s", exc)


def get_server_health(manager: Any) -> dict[str, dict[str, Any]]:
    """Format health status for all known servers."""
    return {
        name: {
            "state": status.state.value,
            "health": status.health.value,
            "last_check": (
                status.last_health_check.isoformat() if status.last_health_check else None
            ),
            "failures": status.consecutive_failures,
            "response_time_ms": status.response_time_ms,
        }
        for name, status in manager.health.items()
    }

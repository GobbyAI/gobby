"""Tool and resource invocation helpers for MCP client manager."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, cast

from opentelemetry.trace import Status, StatusCode

from gobby.telemetry.tracing import create_span


async def call_tool(
    manager: Any,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any] | None,
    timeout: float | None,
    session_id: str | None,
    logger: logging.Logger,
) -> Any:
    """Call a tool on a downstream MCP server with tracing and metrics."""
    start_time = time.perf_counter()
    success = False
    with create_span(
        "mcp.call_tool",
        attributes={"server_name": server_name, "tool_name": tool_name},
    ) as span:
        try:
            session = await manager.get_client_session(server_name)
            if timeout:
                result = await asyncio.wait_for(
                    session.call_tool(tool_name, arguments or {}),
                    timeout=timeout,
                )
            else:
                result = await session.call_tool(tool_name, arguments or {})
            manager.health[server_name].record_success()
            success = True
            if span.is_recording():
                span.set_attribute("success", True)
            return result
        except Exception as exc:
            if server_name in manager.health:
                manager.health[server_name].record_failure(str(exc))
            if span.is_recording():
                span.set_attribute("success", False)
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        finally:
            latency_ms = (time.perf_counter() - start_time) * 1000
            if span.is_recording():
                span.set_attribute("latency_ms", latency_ms)

            if manager.metrics_manager:
                server_config = manager._configs.get(server_name)
                metrics_project_id = server_config.project_id if server_config else manager.project_id
                if metrics_project_id:
                    try:
                        manager.metrics_manager.record_call(
                            server_name=server_name,
                            tool_name=tool_name,
                            project_id=metrics_project_id,
                            latency_ms=latency_ms,
                            success=success,
                            session_id=session_id,
                        )
                    except Exception:
                        logger.debug("Failed to record metrics for %s.%s", server_name, tool_name)


async def read_resource(manager: Any, server_name: str, uri: str) -> Any:
    """Read a resource from a downstream MCP server."""
    try:
        session = await manager.get_client_session(server_name)
        result = await session.read_resource(cast(Any, str(uri)))
        manager.health[server_name].record_success()
        return result
    except Exception as exc:
        if server_name in manager.health:
            manager.health[server_name].record_failure(str(exc))
        raise

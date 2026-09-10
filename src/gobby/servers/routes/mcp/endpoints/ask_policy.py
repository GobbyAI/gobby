"""Ask-specific authorization policy for MCP execution and discovery routes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from gobby.ask.permissions import (
    AskPermissionDenied,
    ask_tool_denial_reason,
    current_ask_allowed_tools,
)
from gobby.ask.stage_authority import stage_tool_is_discoverable
from gobby.mcp_proxy.models import ToolProxyErrorCode

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


def policy_service(server: HTTPServer) -> object:
    return server.tool_proxy or server


def current_allowed_tools(server: HTTPServer) -> frozenset[tuple[str, str]] | None:
    return current_ask_allowed_tools(policy_service(server))


def _normalize_server_alias(server: HTTPServer, server_name: str, tool_name: str) -> str:
    if server_name != "gobby":
        return server_name
    if server.tool_proxy is not None:
        resolved = server.tool_proxy.find_tool_server(tool_name)
    elif server._internal_manager is not None:
        resolved = server._internal_manager.find_tool_server(tool_name)
    else:
        resolved = None
    return resolved or server_name


def filter_tools(
    server_name: str,
    tools: Sequence[Any],
    allowed: frozenset[tuple[str, str]] | None,
) -> list[Any]:
    visible: list[Any] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            if allowed is None:
                visible.append(tool)
            continue
        tool_name = str(tool.get("name"))
        if stage_tool_is_discoverable(server_name, tool_name) and (
            allowed is None or (server_name, tool_name) in allowed
        ):
            visible.append(dict(tool))
    return visible


def filter_tool_map(
    tools_by_server: Mapping[str, Sequence[Any]],
    allowed: frozenset[tuple[str, str]] | None,
) -> dict[str, list[Any]]:
    filtered: dict[str, list[Any]] = {}
    for server_name, tools in tools_by_server.items():
        visible = filter_tools(server_name, tools, allowed)
        if allowed is None or visible:
            filtered[server_name] = visible
    return filtered


def filter_records(
    records: object,
    allowed: frozenset[tuple[str, str]] | None,
    *,
    server_key: str,
    tool_key: str,
) -> list[dict[str, Any]]:
    if not isinstance(records, Sequence) or isinstance(records, str | bytes):
        return []
    mapped = [dict(record) for record in records if isinstance(record, Mapping)]
    return [
        record
        for record in mapped
        if stage_tool_is_discoverable(
            str(record.get(server_key)),
            str(record.get(tool_key)),
        )
        and (allowed is None or (str(record.get(server_key)), str(record.get(tool_key))) in allowed)
    ]


def _denial(server_name: str, tool_name: str, reason: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": reason,
        "error_code": ToolProxyErrorCode.TOOL_BLOCKED.value,
        "server_name": server_name,
        "tool_name": tool_name,
    }


async def schema_denial(
    server: HTTPServer,
    server_name: str,
    tool_name: str,
) -> dict[str, Any] | None:
    server_name = _normalize_server_alias(server, server_name, tool_name)
    if not stage_tool_is_discoverable(server_name, tool_name):
        return _denial(
            server_name,
            tool_name,
            f"Ask internal stage schema requires the owning Ask pipeline: {tool_name}",
        )
    try:
        allowed = await asyncio.to_thread(current_ask_allowed_tools, policy_service(server))
    except AskPermissionDenied as exc:
        reason = str(exc)
    else:
        if allowed is None or (server_name, tool_name) in allowed:
            return None
        reason = f"Ask managed agent cannot discover {server_name}.{tool_name}"
    return _denial(server_name, tool_name, reason)


async def fallback_denial(
    server: HTTPServer,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any] | None:
    server_name = _normalize_server_alias(server, server_name, tool_name)
    reason = await asyncio.to_thread(
        ask_tool_denial_reason,
        policy_service(server),
        server_name,
        tool_name,
        arguments,
    )
    return None if reason is None else _denial(server_name, tool_name, reason)

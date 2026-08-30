"""Utilities for shaping MCP server-list responses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gobby.storage.projects import GLOBAL_PROJECT_ID


def compact_mcp_server_list(
    result: dict[str, Any],
    *,
    include_ids: bool = False,
) -> dict[str, Any]:
    """Compact server details for MCP tool output.

    Each row carries ``name``, ``scope``, ``template``, ``state``, ``transport``,
    and ``enabled``. ``id`` is included only when ``include_ids`` is true.
    """
    if result.get("success") is False:
        return result

    servers = result.get("servers")
    if not isinstance(servers, list):
        return result

    compact_servers: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    for server in servers:
        if isinstance(server, str):
            compact_servers.append({"name": server})
            continue
        if not isinstance(server, Mapping):
            continue

        name = server.get("name")
        if not isinstance(name, str) or not name:
            continue

        state_value = server.get("state", "unknown")
        state = state_value if isinstance(state_value, str) else "unknown"
        enabled = server.get("enabled", True)
        transport = server.get("transport")
        row: dict[str, Any] = {
            "name": name,
            "state": state,
            "enabled": enabled is not False,
        }
        if isinstance(transport, str) and transport:
            row["transport"] = transport
        scope = server.get("scope")
        if isinstance(scope, str) and scope:
            row["scope"] = scope
        elif server.get("project_id") == GLOBAL_PROJECT_ID:
            row["scope"] = "global"
        template = server.get("template")
        if isinstance(template, str) and template:
            row["template"] = template
        if include_ids:
            server_id = server.get("id")
            if isinstance(server_id, str) and server_id:
                row["id"] = server_id
        compact_servers.append(row)

        if state == "connected" and enabled is not False:
            continue

        issue: dict[str, Any] = {"name": name, "state": state}
        if isinstance(transport, str) and transport:
            issue["transport"] = transport
        if enabled is False:
            issue["enabled"] = False
        issues.append(issue)

    compact: dict[str, Any] = {}
    if "success" in result:
        compact["success"] = result["success"]
    compact["servers"] = compact_servers
    if "total" in result:
        compact["total"] = result["total"]
    if "connected" in result:
        compact["connected"] = result["connected"]
    if issues:
        compact["issues"] = issues

    for key, value in result.items():
        if key not in compact and key not in {"servers", "issues"}:
            compact[key] = value

    return compact

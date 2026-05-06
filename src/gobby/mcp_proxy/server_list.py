"""Utilities for shaping MCP server-list responses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def compact_mcp_server_list(result: dict[str, Any]) -> dict[str, Any]:
    """Compact server details for MCP tool output.

    Connected servers are represented by name. Servers that need attention retain
    enough status detail for diagnosis.
    """
    if result.get("success") is False:
        return result

    servers = result.get("servers")
    if not isinstance(servers, list):
        return result

    server_names: list[str] = []
    issues: list[dict[str, Any]] = []

    for server in servers:
        if isinstance(server, str):
            server_names.append(server)
            continue
        if not isinstance(server, Mapping):
            continue

        name = server.get("name")
        if not isinstance(name, str) or not name:
            continue

        server_names.append(name)
        state_value = server.get("state", "unknown")
        state = state_value if isinstance(state_value, str) else "unknown"
        enabled = server.get("enabled", True)

        if state == "connected" and enabled is not False:
            continue

        issue: dict[str, Any] = {"name": name, "state": state}
        transport = server.get("transport")
        if isinstance(transport, str) and transport:
            issue["transport"] = transport
        if enabled is False:
            issue["enabled"] = False
        issues.append(issue)

    compact: dict[str, Any] = {}
    if "success" in result:
        compact["success"] = result["success"]
    compact["servers"] = server_names
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

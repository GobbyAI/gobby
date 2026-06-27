"""ACP authentication metadata helpers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def normalize_auth_methods(auth_methods: Any) -> list[dict[str, Any]]:
    """Return valid ACP auth methods in provider order."""
    if not isinstance(auth_methods, list):
        return []

    normalized: list[dict[str, Any]] = []
    for method in auth_methods:
        if not isinstance(method, dict):
            continue
        method_id = method.get("id")
        name = method.get("name")
        if not isinstance(method_id, str) or not method_id:
            continue
        if not isinstance(name, str) or not name:
            continue

        entry: dict[str, Any] = {
            "id": method_id,
            "name": name,
            "type": method.get("type") if isinstance(method.get("type"), str) else "agent",
        }
        description = method.get("description")
        if isinstance(description, str) and description:
            entry["description"] = description
        normalized.append(deepcopy(entry))

    return normalized


def supports_auth_logout(agent_capabilities: Any) -> bool:
    """Return whether ACP agentCapabilities advertises auth.logout."""
    if not isinstance(agent_capabilities, dict):
        return False
    auth = agent_capabilities.get("auth")
    if not isinstance(auth, dict):
        return False
    return isinstance(auth.get("logout"), dict)

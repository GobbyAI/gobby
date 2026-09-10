"""Parse and merge MCP server configuration at API and registry boundaries."""

from collections.abc import Mapping
from typing import Any

from gobby.mcp_proxy.models import MCPServerConfig


def string_dict(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("env and headers must be JSON objects")
    return {str(key): str(item) for key, item in value.items() if str(key)}


def string_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("args must be a JSON array")
    return [str(item) for item in value]


def optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value)
    return value


def bool_field(body: Mapping[str, Any], name: str, default: bool) -> bool:
    value = body.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def build_mcp_server_config(
    body: Mapping[str, Any],
    *,
    name: str,
    project_id: str,
    base: "MCPServerConfig | None" = None,
) -> "MCPServerConfig":
    connect_timeout = body.get("connect_timeout", 30.0 if base is None else base.connect_timeout)
    if connect_timeout is None:
        connect_timeout = 30.0 if base is None else base.connect_timeout

    config = MCPServerConfig(
        name=name,
        project_id=project_id if base is None else base.project_id,
        transport=str(body.get("transport") or (base.transport if base else "http")),
        url=optional_string(body["url"]) if "url" in body else (base.url if base else None),
        command=optional_string(body["command"])
        if "command" in body
        else (base.command if base else None),
        args=string_list(body["args"]) if "args" in body else (base.args if base else None),
        env=string_dict(body["env"]) if "env" in body else (base.env if base else None),
        headers=string_dict(body["headers"])
        if "headers" in body
        else (base.headers if base else None),
        enabled=bool_field(body, "enabled", True if base is None else base.enabled),
        description=optional_string(body["description"])
        if "description" in body
        else (base.description if base else None),
        requires_oauth=bool_field(
            body, "requires_oauth", False if base is None else base.requires_oauth
        ),
        oauth_provider=optional_string(body.get("oauth_provider"))
        if "oauth_provider" in body
        else (base.oauth_provider if base else None),
        connect_timeout=float(connect_timeout),
    )
    if base is not None:
        config.id = base.id
        config.tools = base.tools
        config.template_id = base.template_id
        config.template = base.template
        config.runtime_hook = base.runtime_hook
        config.template_values = dict(base.template_values or {})
    config.validate()
    return config

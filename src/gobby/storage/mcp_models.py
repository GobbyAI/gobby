"""MCP storage row models."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gobby.utils.datetime import normalize_datetime_model


def _loads_server_json_field(row: Mapping[str, Any], field: str) -> Any:
    raw = row.get(field)
    if raw is None:
        return None
    if isinstance(raw, str):
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            server_id = row.get("id", "<unknown>")
            raise ValueError(
                f"Invalid JSON for MCP server {server_id} field {field}: {exc}"
            ) from exc
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        server_id = row.get("id", "<unknown>")
        raise ValueError(f"Invalid JSON for MCP server {server_id} field {field}: {exc}") from exc


def _loads_tool_input_schema(row: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = row["input_schema"]
    if raw is None:
        return None
    if isinstance(raw, dict):
        schema = raw
    elif isinstance(raw, str):
        if not raw:
            return None
        try:
            schema = json.loads(raw)
        except json.JSONDecodeError as exc:
            tool_id = row.get("id", "<unknown>")
            tool_name = row.get("name", "<unknown>")
            raise ValueError(
                f"Invalid JSON for MCP tool {tool_name} ({tool_id}) input_schema: {exc}"
            ) from exc
    else:
        schema = raw
    if not isinstance(schema, dict):
        tool_id = row.get("id", "<unknown>")
        tool_name = row.get("name", "<unknown>")
        raise ValueError(
            f"Invalid JSON for MCP tool {tool_name} ({tool_id}) input_schema: "
            f"expected object, got {type(schema).__name__}"
        )
    return schema


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    )
)
@dataclass
class MCPServer:
    """MCP server configuration model."""

    id: str
    name: str
    transport: str
    url: str | None
    command: str | None
    args: list[str] | None
    env: dict[str, str] | None
    headers: dict[str, str] | None
    enabled: bool
    description: str | None
    requires_oauth: bool
    oauth_provider: str | None
    connect_timeout: float
    created_at: datetime
    updated_at: datetime
    project_id: str  # Required - all servers must belong to a project
    template_id: str | None = None
    template_values: dict[str, Any] | None = None
    runtime_hook: str | None = None
    template: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MCPServer":
        """Create MCPServer from database row."""
        connect_timeout = row.get("connect_timeout")
        template_id = row.get("template_id")
        template = row.get("template")
        return cls(
            id=row["id"],
            name=row["name"],
            transport=row["transport"],
            url=row["url"],
            command=row["command"],
            args=_loads_server_json_field(row, "args"),
            env=_loads_server_json_field(row, "env"),
            headers=_loads_server_json_field(row, "headers"),
            enabled=bool(row["enabled"]),
            description=row["description"],
            requires_oauth=bool(row.get("requires_oauth", False)),
            oauth_provider=row.get("oauth_provider"),
            connect_timeout=float(connect_timeout) if connect_timeout is not None else 30.0,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            project_id=row["project_id"],
            template_id=str(template_id) if template_id is not None else None,
            template_values=_loads_server_json_field(row, "template_values"),
            runtime_hook=row.get("runtime_hook"),
            template=str(template) if template is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "project_id": self.project_id,
            "transport": self.transport,
            "url": self.url,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "headers": self.headers,
            "enabled": self.enabled,
            "description": self.description,
            "requires_oauth": self.requires_oauth,
            "oauth_provider": self.oauth_provider,
            "connect_timeout": self.connect_timeout,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "template_id": self.template_id,
            "template_values": self.template_values,
            "runtime_hook": self.runtime_hook,
            "template": self.template,
        }

    def to_config(self) -> dict[str, Any]:
        """Convert to MCP config format."""
        config: dict[str, Any] = {
            "name": self.name,
            "project_id": self.project_id,
            "transport": self.transport,
            "enabled": self.enabled,
        }
        if self.url:
            config["url"] = self.url
        if self.command:
            config["command"] = self.command
        if self.args:
            config["args"] = self.args
        if self.env:
            config["env"] = self.env
        if self.headers:
            config["headers"] = self.headers
        if self.description:
            config["description"] = self.description
        config["requires_oauth"] = self.requires_oauth
        if self.oauth_provider:
            config["oauth_provider"] = self.oauth_provider
        config["connect_timeout"] = self.connect_timeout
        config["id"] = self.id
        config["template_id"] = self.template_id
        config["template"] = self.template
        config["runtime_hook"] = self.runtime_hook
        config["template_values"] = self.template_values
        return config


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    )
)
@dataclass
class Tool:
    """MCP tool model."""

    id: str
    mcp_server_id: str
    name: str
    description: str | None
    input_schema: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Tool":
        """Create Tool from database row."""
        return cls(
            id=row["id"],
            mcp_server_id=row["mcp_server_id"],
            name=row["name"],
            description=row["description"],
            input_schema=_loads_tool_input_schema(row),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "mcp_server_id": self.mcp_server_id,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

"""MCP template listing endpoint."""

from typing import TYPE_CHECKING, Any

from fastapi import Depends, Request

from gobby.mcp_proxy.templates import MCPServerTemplate
from gobby.servers.routes.dependencies import get_server
from gobby.servers.routes.mcp.endpoints.request_context import request_mcp_scope
from gobby.storage.projects import GLOBAL_PROJECT_ID

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


def _scope_label(project_id: str) -> str:
    return "global" if project_id == GLOBAL_PROJECT_ID else "project"


async def list_mcp_templates(
    request: Request,
    server: "HTTPServer" = Depends(get_server),
) -> dict[str, Any]:
    """List templates visible to the resolved project."""
    scope_project = request_mcp_scope(request, server, None)
    db = getattr(server.mcp_manager, "mcp_db_manager", None) if server.mcp_manager else None
    if db is None:
        db = getattr(server, "_mcp_db_manager", None)
    if db is None or not hasattr(db, "list_templates"):
        return {"success": True, "templates": []}
    rows = db.list_templates(project_id=scope_project, enabled_only=False)
    templates: list[dict[str, Any]] = []
    for row in rows:
        definition = dict(getattr(row, "definition", None) or {})
        try:
            parsed = MCPServerTemplate.from_definition({**definition, "name": row.name})
            params = [
                {
                    "name": param.name,
                    "required": param.required,
                    "secret": param.secret,
                    "env": param.env,
                    "arg_flag": param.arg_flag,
                    "choices": list(param.choices),
                    "description": param.description,
                }
                for param in parsed.params
            ]
            description = parsed.description
        except ValueError:
            params = list(definition.get("params") or [])
            description = definition.get("description") or ""
        templates.append(
            {
                "name": row.name,
                "description": description,
                "owner": getattr(row, "owner", None),
                "scope": _scope_label(str(getattr(row, "project_id", ""))),
                "params": params,
            }
        )
    return {"success": True, "templates": templates}

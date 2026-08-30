"""GitHub MCP helpers for source-control routes."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from gobby.integrations.github import GitHubIntegration
from gobby.mcp_proxy.services.server_resolution import resolve_server
from gobby.storage.projects import GLOBAL_PROJECT_ID

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def _get_github(server: HTTPServer, project_id: str | None) -> GitHubIntegration | None:
    """Get a GitHubIntegration scoped to ``project_id`` (global when None)."""
    if server.services.mcp_manager:
        return GitHubIntegration(
            server.services.mcp_manager,
            project_id=project_id or GLOBAL_PROJECT_ID,
        )
    return None


async def _call_github_mcp(
    server: HTTPServer,
    project_id: str | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> Any:
    """Call a tool on the GitHub MCP instance for ``project_id``."""
    manager = server.services.mcp_manager
    if not manager:
        raise HTTPException(503, "MCP manager not available")

    scope = project_id or GLOBAL_PROJECT_ID
    config = resolve_server(manager, "github", project_id=scope)
    if config is None:
        raise HTTPException(
            404,
            {"success": False, "error": f"Server 'github' not found in project scope {scope}"},
        )
    try:
        session = await manager.get_client_session(config.id)
        result = await session.call_tool(tool_name, arguments)
        if hasattr(result, "content") and result.content:
            import json

            for item in result.content:
                if hasattr(item, "text"):
                    try:
                        return json.loads(item.text)
                    except (json.JSONDecodeError, TypeError):
                        return item.text
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("GitHub MCP call failed (%s): %s", tool_name, e, exc_info=True)
        raise HTTPException(502, "GitHub MCP call failed") from e


def _parse_github_repo(github_repo: str | None) -> tuple[str, str] | None:
    """Parse 'owner/repo' string into (owner, repo) tuple."""
    if not github_repo or "/" not in github_repo:
        return None
    parts = github_repo.split("/", 1)
    return parts[0], parts[1]

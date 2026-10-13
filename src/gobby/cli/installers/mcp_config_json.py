"""JSON and Claude project MCP config operations."""

from pathlib import Path
from typing import Any

from gobby.claude_config import claude_config_path, update_claude_config

from .mcp_config_shared import (
    _GOBBY_MCP_ARGS,
    _facade_copy2,
    _facade_time,
    _is_repairable_stale_gobby_mcp_server_config,
    _resolved_gobby_mcp_command,
)


def _mcp_scope(settings: dict[str, Any], project_key: str | None) -> dict[str, Any]:
    if project_key is None:
        return settings
    projects = settings.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("projects must be a JSON object")
    project = projects.setdefault(project_key, {})
    if not isinstance(project, dict):
        raise ValueError("project settings must be a JSON object")
    return project


def _backup(settings_path: Path, result: dict[str, Any]) -> None:
    if settings_path.exists():
        timestamp = int(_facade_time().time())
        backup_path = settings_path.parent / f"{settings_path.name}.{timestamp}.backup"
        try:
            _facade_copy2(settings_path, backup_path)
        except OSError as exc:
            raise OSError(f"Failed to create backup: {exc}") from exc
        result["backup_path"] = str(backup_path)


def _configure(
    settings_path: Path,
    server_name: str,
    project_key: str | None = None,
    extra_server_fields: dict[str, Any] | None = None,
    *,
    follow_provider_symlink: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": False,
        "added": False,
        "updated": False,
        "already_configured": False,
        "backup_path": None,
        "error": None,
    }
    action = "already_configured"

    def configure(settings: dict[str, Any]) -> bool:
        nonlocal action
        scope = _mcp_scope(settings, project_key)
        servers = scope.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcpServers must be a JSON object")
        if server_name in servers:
            server = servers[server_name]
            updates: dict[str, Any] = {}
            if isinstance(server, dict):
                if _is_repairable_stale_gobby_mcp_server_config(server):
                    updates.update(command=_resolved_gobby_mcp_command(), args=[*_GOBBY_MCP_ARGS])
                if extra_server_fields:
                    updates.update(
                        {
                            key: value
                            for key, value in extra_server_fields.items()
                            if server.get(key) != value
                        }
                    )
                if updates:
                    _backup(settings_path, result)
                    server.update(updates)
                    action = "updated"
                    return True
            return False
        _backup(settings_path, result)
        server = {"command": _resolved_gobby_mcp_command(), "args": [*_GOBBY_MCP_ARGS]}
        if project_key is not None:
            server["type"] = "stdio"
        if extra_server_fields:
            server.update(extra_server_fields)
        servers[server_name] = server
        action = "added"
        return True

    try:
        writer_path = settings_path
        if follow_provider_symlink and settings_path.is_symlink():
            writer_path = settings_path.resolve()
            if not writer_path.parent.is_dir():
                raise FileNotFoundError(
                    f"MCP target parent is not a directory: {writer_path.parent}"
                )
        update_claude_config(writer_path, configure)
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
        return result
    result["success"] = True
    result[action] = True
    return result


def _remove(
    settings_path: Path,
    server_name: str,
    project_key: str | None = None,
    *,
    follow_provider_symlink: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": False,
        "removed": False,
        "backup_path": None,
        "error": None,
    }
    if not settings_path.exists() and (follow_provider_symlink or not settings_path.is_symlink()):
        result["success"] = True
        return result

    def remove(settings: dict[str, Any]) -> bool:
        scope = _mcp_scope(settings, project_key)
        servers = scope.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcpServers must be a JSON object")
        if server_name not in servers:
            return False
        _backup(settings_path, result)
        del servers[server_name]
        if not servers:
            del scope["mcpServers"]
        return True

    try:
        writer_path = (
            settings_path.resolve()
            if follow_provider_symlink and settings_path.is_symlink()
            else settings_path
        )
        changed = update_claude_config(writer_path, remove)
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
        return result
    result["success"] = True
    result["removed"] = changed
    return result


def configure_project_mcp_server(project_path: Path, server_name: str = "gobby") -> dict[str, Any]:
    """Configure project-scoped MCP servers in the selected host Claude config."""
    return _configure(claude_config_path(), server_name, str(project_path.resolve()))


def remove_project_mcp_server(project_path: Path, server_name: str = "gobby") -> dict[str, Any]:
    """Remove a project-scoped MCP server from the selected host Claude config."""
    return _remove(claude_config_path(), server_name, str(project_path.resolve()))


def configure_mcp_server_json(
    settings_path: Path,
    server_name: str = "gobby",
    *,
    extra_server_fields: dict[str, Any] | None = None,
    follow_provider_symlink: bool = False,
) -> dict[str, Any]:
    """Configure an MCP server, backing up and preserving unrelated settings."""
    return _configure(
        settings_path,
        server_name,
        extra_server_fields=extra_server_fields,
        follow_provider_symlink=follow_provider_symlink,
    )


def remove_mcp_server_json(
    settings_path: Path,
    server_name: str = "gobby",
    *,
    follow_provider_symlink: bool = False,
) -> dict[str, Any]:
    """Remove an MCP server, backing up and preserving unrelated settings."""
    return _remove(settings_path, server_name, follow_provider_symlink=follow_provider_symlink)

"""JSON and Claude project MCP config operations."""

import json
import sys
from pathlib import Path
from typing import Any

from .mcp_config_shared import (
    _GOBBY_MCP_ARGS,
    _GOBBY_MCP_COMMAND,
    _facade_copy2,
    _facade_time,
    _is_repairable_stale_gobby_mcp_server_config,
)


def _resolved_gobby_mcp_command() -> str:
    gobby_bin = Path(sys.executable).parent / "gobby"
    if gobby_bin.exists():
        return str(gobby_bin)
    return _GOBBY_MCP_COMMAND


def _load_json_object(
    settings_path: Path,
    result: dict[str, Any],
    *,
    parse_error_prefix: str,
    read_error_prefix: str,
) -> dict[str, Any] | None:
    try:
        with open(settings_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        result["error"] = f"{parse_error_prefix} {settings_path}: {e}"
        return None
    except OSError as e:
        result["error"] = f"{read_error_prefix} {settings_path}: {e}"
        return None
    if not isinstance(data, dict):
        result["error"] = (
            f"{parse_error_prefix} {settings_path}: expected JSON object, got {type(data).__name__}"
        )
        return None
    return data


def configure_project_mcp_server(project_path: Path, server_name: str = "gobby") -> dict[str, Any]:
    """Add Gobby MCP server to project-specific config in ~/.claude.json.

    Claude Code stores project-specific MCP servers in:
    {
      "projects": {
        "/path/to/project": {
          "mcpServers": { "gobby": { ... } }
        }
      }
    }

    Args:
        project_path: Path to the project root
        server_name: Name for the MCP server entry (default: "gobby")

    Returns:
        Dict with 'success', 'added', 'already_configured', 'backup_path', and 'error' keys
    """
    result: dict[str, Any] = {
        "success": False,
        "added": False,
        "updated": False,
        "already_configured": False,
        "backup_path": None,
        "error": None,
    }

    settings_path = Path.home() / ".claude.json"
    abs_project_path = str(project_path.resolve())

    # Load existing settings or create empty
    existing_settings: dict[str, Any] = {}
    if settings_path.exists():
        loaded = _load_json_object(
            settings_path,
            result,
            parse_error_prefix="Failed to parse",
            read_error_prefix="Failed to read",
        )
        if loaded is None:
            return result
        existing_settings = loaded

    # Ensure projects section exists
    if "projects" not in existing_settings:
        existing_settings["projects"] = {}

    # Ensure project entry exists
    if abs_project_path not in existing_settings["projects"]:
        existing_settings["projects"][abs_project_path] = {}

    project_settings = existing_settings["projects"][abs_project_path]

    # Ensure mcpServers section exists in project
    if "mcpServers" not in project_settings:
        project_settings["mcpServers"] = {}

    # Check if already configured
    if server_name in project_settings["mcpServers"]:
        server_config = project_settings["mcpServers"][server_name]
        if isinstance(server_config, dict) and _is_repairable_stale_gobby_mcp_server_config(
            server_config
        ):
            if settings_path.exists():
                timestamp = int(_facade_time().time())
                backup_path = settings_path.parent / f".claude.json.{timestamp}.backup"
                try:
                    _facade_copy2(settings_path, backup_path)
                    result["backup_path"] = str(backup_path)
                except OSError as e:
                    result["error"] = f"Failed to create backup: {e}"
                    return result

            server_config["command"] = _resolved_gobby_mcp_command()
            server_config["args"] = [*_GOBBY_MCP_ARGS]
            try:
                with open(settings_path, "w", encoding="utf-8") as f:
                    json.dump(existing_settings, f, indent=2)
            except OSError as e:
                result["error"] = f"Failed to write {settings_path}: {e}"
                return result

            result["success"] = True
            result["updated"] = True
            return result

        result["success"] = True
        result["already_configured"] = True
        return result

    # Create backup if file exists
    if settings_path.exists():
        timestamp = int(_facade_time().time())
        backup_path = settings_path.parent / f".claude.json.{timestamp}.backup"
        try:
            _facade_copy2(settings_path, backup_path)
            result["backup_path"] = str(backup_path)
        except OSError as e:
            result["error"] = f"Failed to create backup: {e}"
            return result

    # Add gobby MCP server config
    project_settings["mcpServers"][server_name] = {
        "type": "stdio",
        "command": _resolved_gobby_mcp_command(),
        "args": [*_GOBBY_MCP_ARGS],
    }

    # Write updated settings
    try:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(existing_settings, f, indent=2)
    except OSError as e:
        result["error"] = f"Failed to write {settings_path}: {e}"
        return result

    result["success"] = True
    result["added"] = True
    return result


def remove_project_mcp_server(project_path: Path, server_name: str = "gobby") -> dict[str, Any]:
    """Remove Gobby MCP server from project-specific config in ~/.claude.json.

    Args:
        project_path: Path to the project root
        server_name: Name of the MCP server entry to remove

    Returns:
        Dict with 'success', 'removed', 'backup_path', and 'error' keys
    """
    result: dict[str, Any] = {
        "success": False,
        "removed": False,
        "backup_path": None,
        "error": None,
    }

    settings_path = Path.home() / ".claude.json"
    abs_project_path = str(project_path.resolve())

    if not settings_path.exists():
        result["success"] = True
        return result

    settings = _load_json_object(
        settings_path,
        result,
        parse_error_prefix="Failed to parse",
        read_error_prefix="Failed to read",
    )
    if settings is None:
        return result

    # Check if project and server exist
    projects = settings.get("projects", {})
    project_settings = projects.get(abs_project_path, {})
    mcp_servers = project_settings.get("mcpServers", {})

    if server_name not in mcp_servers:
        result["success"] = True
        return result

    # Create backup
    timestamp = int(_facade_time().time())
    backup_path = settings_path.parent / f".claude.json.{timestamp}.backup"
    try:
        _facade_copy2(settings_path, backup_path)
        result["backup_path"] = str(backup_path)
    except OSError as e:
        result["error"] = f"Failed to create backup: {e}"
        return result

    # Remove the server
    del mcp_servers[server_name]

    # Write updated settings
    try:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except OSError as e:
        result["error"] = f"Failed to write {settings_path}: {e}"
        return result

    result["success"] = True
    result["removed"] = True
    return result


def configure_mcp_server_json(
    settings_path: Path,
    server_name: str = "gobby",
    *,
    extra_server_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add Gobby MCP server to a JSON settings file (Claude, AGY).

    Merges the gobby MCP server config into the existing mcpServers section,
    preserving all other servers. Creates a timestamped backup before modifying.

    Args:
        settings_path: Path to the settings.json file (e.g., ~/.claude/settings.json)
        server_name: Name for the MCP server entry (default: "gobby")
        extra_server_fields: Optional fields to merge into the server entry.

    Returns:
        Dict with 'success', 'added', 'backup_path', and 'error' keys
    """
    result: dict[str, Any] = {
        "success": False,
        "added": False,
        "already_configured": False,
        "backup_path": None,
        "error": None,
        "updated": False,
    }

    # Ensure parent directory exists
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings or create empty
    existing_settings: dict[str, Any] = {}
    if settings_path.exists():
        loaded = _load_json_object(
            settings_path,
            result,
            parse_error_prefix="Failed to parse",
            read_error_prefix="Failed to read",
        )
        if loaded is None:
            return result
        existing_settings = loaded

    # Check if already configured. Existing callers preserve the historical
    # "presence means configured" behavior; callers that pass extra fields can
    # ask us to merge those fields into an existing server. Known stale
    # `uv run ... gobby mcp-server` entries are repaired because they can launch
    # the stdio wrapper from the wrong project context.
    if "mcpServers" in existing_settings and server_name in existing_settings["mcpServers"]:
        server_config = existing_settings["mcpServers"][server_name]
        if isinstance(server_config, dict):
            updates: dict[str, Any] = {}
            if _is_repairable_stale_gobby_mcp_server_config(server_config):
                updates["command"] = _resolved_gobby_mcp_command()
                updates["args"] = [*_GOBBY_MCP_ARGS]
            if extra_server_fields:
                updates.update(
                    {
                        key: value
                        for key, value in extra_server_fields.items()
                        if server_config.get(key) != value
                    }
                )
            if updates:
                if settings_path.exists():
                    timestamp = int(_facade_time().time())
                    backup_path = settings_path.parent / f"{settings_path.name}.{timestamp}.backup"
                    try:
                        _facade_copy2(settings_path, backup_path)
                        result["backup_path"] = str(backup_path)
                    except OSError as e:
                        result["error"] = f"Failed to create backup: {e}"
                        return result

                server_config.update(updates)
                try:
                    with open(settings_path, "w", encoding="utf-8") as f:
                        json.dump(existing_settings, f, indent=2)
                except OSError as e:
                    result["error"] = f"Failed to write {settings_path}: {e}"
                    return result

                result["success"] = True
                result["updated"] = True
                return result

        result["success"] = True
        result["already_configured"] = True
        return result

    # Create backup if file exists
    if settings_path.exists():
        timestamp = int(_facade_time().time())
        backup_path = settings_path.parent / f"{settings_path.name}.{timestamp}.backup"
        try:
            _facade_copy2(settings_path, backup_path)
            result["backup_path"] = str(backup_path)
        except OSError as e:
            result["error"] = f"Failed to create backup: {e}"
            return result

    # Ensure mcpServers section exists
    if "mcpServers" not in existing_settings:
        existing_settings["mcpServers"] = {}

    # Add gobby MCP server config.
    server_config = {
        "command": _resolved_gobby_mcp_command(),
        "args": [*_GOBBY_MCP_ARGS],
    }
    if extra_server_fields:
        server_config.update(extra_server_fields)
    existing_settings["mcpServers"][server_name] = server_config

    # Write updated settings
    try:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(existing_settings, f, indent=2)
    except OSError as e:
        result["error"] = f"Failed to write {settings_path}: {e}"
        return result

    result["success"] = True
    result["added"] = True
    return result


def remove_mcp_server_json(settings_path: Path, server_name: str = "gobby") -> dict[str, Any]:
    """Remove Gobby MCP server from a JSON settings file.

    Args:
        settings_path: Path to the settings.json file
        server_name: Name of the MCP server entry to remove

    Returns:
        Dict with 'success', 'removed', 'backup_path', and 'error' keys
    """
    result: dict[str, Any] = {
        "success": False,
        "removed": False,
        "backup_path": None,
        "error": None,
    }

    if not settings_path.exists():
        result["success"] = True
        return result

    settings = _load_json_object(
        settings_path,
        result,
        parse_error_prefix="Failed to parse",
        read_error_prefix="Failed to read",
    )
    if settings is None:
        return result

    # Check if server exists
    if "mcpServers" not in settings or server_name not in settings["mcpServers"]:
        result["success"] = True
        return result

    # Create backup
    timestamp = int(_facade_time().time())
    backup_path = settings_path.parent / f"{settings_path.name}.{timestamp}.backup"
    try:
        _facade_copy2(settings_path, backup_path)
        result["backup_path"] = str(backup_path)
    except OSError as e:
        result["error"] = f"Failed to create backup: {e}"
        return result

    # Remove the server
    del settings["mcpServers"][server_name]

    # Clean up empty mcpServers section
    if not settings["mcpServers"]:
        del settings["mcpServers"]

    # Write updated settings
    try:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except OSError as e:
        result["error"] = f"Failed to write {settings_path}: {e}"
        return result

    result["success"] = True
    result["removed"] = True
    return result

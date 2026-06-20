"""TOML MCP config operations."""

import re
from pathlib import Path
from typing import Any, cast

from .mcp_config_shared import (
    _CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC,
    _GOBBY_MCP_COMMAND,
    _facade_time,
    _remove_toml_table_block,
    _repair_stale_gobby_mcp_server_toml,
)


def configure_mcp_server_toml(config_path: Path, server_name: str = "gobby") -> dict[str, Any]:
    """Add Gobby MCP server to a TOML config file (Codex).

    Adds [mcp_servers.gobby] section with command and args.
    Creates a timestamped backup before modifying.

    Args:
        config_path: Path to the config.toml file (e.g., ~/.codex/config.toml)
        server_name: Name for the MCP server entry (default: "gobby")

    Returns:
        Dict with 'success', 'added', 'backup_path', and 'error' keys
    """
    result: dict[str, Any] = {
        "success": False,
        "added": False,
        "updated": False,
        "already_configured": False,
        "backup_path": None,
        "error": None,
    }

    # Ensure parent directory exists
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing config
    existing = ""
    if config_path.exists():
        try:
            existing = config_path.read_text(encoding="utf-8")
        except OSError as e:
            result["error"] = f"Failed to read {config_path}: {e}"
            return result

    # Check if already configured
    pattern = re.compile(rf"^\s*\[mcp_servers\.{re.escape(server_name)}\]", re.MULTILINE)
    if pattern.search(existing):
        updated, repair_error = _repair_stale_gobby_mcp_server_toml(
            existing,
            server_name=server_name,
        )
        if repair_error:
            result["error"] = repair_error
            return result
        if updated is not None:
            timestamp = int(_facade_time().time())
            backup_path = config_path.with_suffix(f".toml.{timestamp}.backup")
            try:
                backup_path.write_text(existing, encoding="utf-8")
                result["backup_path"] = str(backup_path)
            except OSError as e:
                result["error"] = f"Failed to create backup: {e}"
                return result

            try:
                config_path.write_text(updated, encoding="utf-8")
            except OSError as e:
                result["error"] = f"Failed to write {config_path}: {e}"
                return result

            result["success"] = True
            result["updated"] = True
            return result

        result["success"] = True
        result["already_configured"] = True
        return result

    # Create backup if file exists
    if config_path.exists():
        timestamp = int(_facade_time().time())
        backup_path = config_path.with_suffix(f".toml.{timestamp}.backup")
        try:
            backup_path.write_text(existing, encoding="utf-8")
            result["backup_path"] = str(backup_path)
        except OSError as e:
            result["error"] = f"Failed to create backup: {e}"
            return result

    # Add MCP server config. Codex should launch gobby from the caller's project
    # environment so the stdio wrapper can derive the correct project scope.
    mcp_config = f"""
[mcp_servers.{server_name}]
command = "{_GOBBY_MCP_COMMAND}"
args = ["mcp-server"]
tool_timeout_sec = {_CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC}
"""
    updated = (existing.rstrip() + "\n" if existing.strip() else "") + mcp_config

    try:
        config_path.write_text(updated, encoding="utf-8")
    except OSError as e:
        result["error"] = f"Failed to write {config_path}: {e}"
        return result

    result["success"] = True
    result["added"] = True
    return result


def strip_mcp_tool_overrides_toml(config_path: Path, server_name: str = "gobby") -> dict[str, Any]:
    """Remove per-tool approval overrides from an MCP server entry in a TOML config.

    Strips the [mcp_servers.<server_name>.tools] sub-table if present,
    so that tool approval inherits the session's approval mode instead of
    being forced to a specific value (e.g. "approve").

    Uses tomlkit for round-trip parsing so comments and formatting survive
    installer cleanup.

    Args:
        config_path: Path to the config.toml file (e.g., ~/.codex/config.toml)
        server_name: Name of the MCP server entry (default: "gobby")

    Returns:
        Dict with 'success', 'stripped', 'backup_path', and 'error' keys
    """
    import tomlkit

    result: dict[str, Any] = {
        "success": False,
        "stripped": False,
        "backup_path": None,
        "error": None,
    }

    if not config_path.exists():
        result["success"] = True
        return result

    # Read and parse TOML (single read; reuse buffer for backup + parse)
    try:
        existing_text = config_path.read_text(encoding="utf-8")
        config = tomlkit.parse(existing_text)
    except tomlkit.exceptions.ParseError as e:
        result["error"] = f"Failed to parse TOML {config_path}: {e}"
        return result
    except OSError as e:
        result["error"] = f"Failed to read {config_path}: {e}"
        return result

    # Check if server exists and has tools sub-table
    server_config = config.get("mcp_servers", {}).get(server_name, {})
    if "tools" not in server_config:
        result["success"] = True
        return result

    # Create backup
    timestamp = int(_facade_time().time())
    backup_path = config_path.with_suffix(f".toml.{timestamp}.backup")
    try:
        backup_path.write_text(existing_text, encoding="utf-8")
        result["backup_path"] = str(backup_path)
    except OSError as e:
        result["error"] = f"Failed to create backup: {e}"
        return result

    # Remove the tools sub-table
    mcp_servers = cast(dict[str, Any], config["mcp_servers"])
    server_config = cast(dict[str, Any], mcp_servers[server_name])
    server_config.pop("tools", None)

    # Write updated config
    try:
        config_path.write_text(tomlkit.dumps(config), encoding="utf-8")
    except OSError as e:
        result["error"] = f"Failed to write {config_path}: {e}"
        return result

    result["success"] = True
    result["stripped"] = True
    return result


def remove_mcp_server_toml(config_path: Path, server_name: str = "gobby") -> dict[str, Any]:
    """Remove Gobby MCP server from a TOML config file.

    Uses tomlkit for round-trip parsing so comments and formatting survive
    installer cleanup.

    Args:
        config_path: Path to the config.toml file
        server_name: Name of the MCP server entry to remove

    Returns:
        Dict with 'success', 'removed', 'backup_path', and 'error' keys
    """
    import tomlkit

    result: dict[str, Any] = {
        "success": False,
        "removed": False,
        "backup_path": None,
        "error": None,
    }

    if not config_path.exists():
        result["success"] = True
        return result

    # Read existing TOML file (single read; reuse buffer for backup + parse)
    try:
        existing_text = config_path.read_text(encoding="utf-8")
        config = tomlkit.parse(existing_text)
    except tomlkit.exceptions.ParseError as e:
        result["error"] = f"Failed to parse TOML {config_path}: {e}"
        return result
    except OSError as e:
        result["error"] = f"Failed to read {config_path}: {e}"
        return result

    # Check if server exists in mcp_servers section
    mcp_servers = config.get("mcp_servers", {})
    if server_name not in mcp_servers:
        result["success"] = True
        return result

    # Create backup
    timestamp = int(_facade_time().time())
    backup_path = config_path.with_suffix(f".toml.{timestamp}.backup")
    try:
        backup_path.write_text(existing_text, encoding="utf-8")
        result["backup_path"] = str(backup_path)
    except OSError as e:
        result["error"] = f"Failed to create backup: {e}"
        return result

    # Remove the server from config while preserving user comments/spacing.
    updated_text = _remove_toml_table_block(
        existing_text, table_prefix=f"mcp_servers.{server_name}"
    )
    try:
        config = tomlkit.parse(updated_text)
    except tomlkit.exceptions.ParseError as e:
        result["error"] = f"Failed to parse updated TOML {config_path}: {e}"
        return result

    # Clean up empty mcp_servers section if the removed server was the last entry.
    mcp_servers = config.get("mcp_servers")
    if mcp_servers is not None and not mcp_servers:
        del config["mcp_servers"]

    try:
        config_path.write_text(tomlkit.dumps(config), encoding="utf-8")
    except OSError as e:
        result["error"] = f"Failed to write {config_path}: {e}"
        return result

    result["success"] = True
    result["removed"] = True
    return result

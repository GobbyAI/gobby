"""
MCP server configuration functions for Gobby installers.

Extracted from shared.py as part of Strangler Fig decomposition (Wave 2).
Handles configuring/removing MCP server entries in JSON and TOML config files.
"""

import json
import logging
import sys
import time
from pathlib import Path
from shutil import copy2
from typing import Any

logger = logging.getLogger(__name__)


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
        "already_configured": False,
        "backup_path": None,
        "error": None,
    }

    settings_path = Path.home() / ".claude.json"
    abs_project_path = str(project_path.resolve())

    # Load existing settings or create empty
    existing_settings: dict[str, Any] = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                existing_settings = json.load(f)
        except json.JSONDecodeError as e:
            result["error"] = f"Failed to parse {settings_path}: {e}"
            return result
        except OSError as e:
            result["error"] = f"Failed to read {settings_path}: {e}"
            return result

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
        result["success"] = True
        result["already_configured"] = True
        return result

    # Create backup if file exists
    if settings_path.exists():
        timestamp = int(time.time())
        backup_path = settings_path.parent / f".claude.json.{timestamp}.backup"
        try:
            copy2(settings_path, backup_path)
            result["backup_path"] = str(backup_path)
        except OSError as e:
            result["error"] = f"Failed to create backup: {e}"
            return result

    # Add gobby MCP server config
    project_settings["mcpServers"][server_name] = {
        "type": "stdio",
        "command": "uv",
        "args": ["run", "gobby", "mcp-server"],
    }

    # Write updated settings
    try:
        with open(settings_path, "w") as f:
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

    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        result["error"] = f"Failed to read {settings_path}: {e}"
        return result

    # Check if project and server exist
    projects = settings.get("projects", {})
    project_settings = projects.get(abs_project_path, {})
    mcp_servers = project_settings.get("mcpServers", {})

    if server_name not in mcp_servers:
        result["success"] = True
        return result

    # Create backup
    timestamp = int(time.time())
    backup_path = settings_path.parent / f".claude.json.{timestamp}.backup"
    try:
        copy2(settings_path, backup_path)
        result["backup_path"] = str(backup_path)
    except OSError as e:
        result["error"] = f"Failed to create backup: {e}"
        return result

    # Remove the server
    del mcp_servers[server_name]

    # Write updated settings
    try:
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
    except OSError as e:
        result["error"] = f"Failed to write {settings_path}: {e}"
        return result

    result["success"] = True
    result["removed"] = True
    return result


def configure_mcp_server_json(settings_path: Path, server_name: str = "gobby") -> dict[str, Any]:
    """Add Gobby MCP server to a JSON settings file (Claude, Gemini, Codex).

    Merges the gobby MCP server config into the existing mcpServers section,
    preserving all other servers. Creates a timestamped backup before modifying.

    Args:
        settings_path: Path to the settings.json file (e.g., ~/.claude/settings.json)
        server_name: Name for the MCP server entry (default: "gobby")

    Returns:
        Dict with 'success', 'added', 'backup_path', and 'error' keys
    """
    result: dict[str, Any] = {
        "success": False,
        "added": False,
        "already_configured": False,
        "backup_path": None,
        "error": None,
    }

    # Ensure parent directory exists
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings or create empty
    existing_settings: dict[str, Any] = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                existing_settings = json.load(f)
        except json.JSONDecodeError as e:
            result["error"] = f"Failed to parse {settings_path}: {e}"
            return result
        except OSError as e:
            result["error"] = f"Failed to read {settings_path}: {e}"
            return result

    # Check if already configured
    if "mcpServers" in existing_settings and server_name in existing_settings["mcpServers"]:
        result["success"] = True
        result["already_configured"] = True
        return result

    # Create backup if file exists
    if settings_path.exists():
        timestamp = int(time.time())
        backup_path = settings_path.parent / f"{settings_path.name}.{timestamp}.backup"
        try:
            copy2(settings_path, backup_path)
            result["backup_path"] = str(backup_path)
        except OSError as e:
            result["error"] = f"Failed to create backup: {e}"
            return result

    # Ensure mcpServers section exists
    if "mcpServers" not in existing_settings:
        existing_settings["mcpServers"] = {}

    # Add gobby MCP server config
    # Resolve the gobby binary from the same environment running this install,
    # so the MCP config survives uv tool reinstalls and PATH changes.
    gobby_bin = str(Path(sys.executable).parent / "gobby")
    if Path(gobby_bin).exists():
        existing_settings["mcpServers"][server_name] = {
            "command": gobby_bin,
            "args": ["mcp-server"],
        }
    else:
        existing_settings["mcpServers"][server_name] = {
            "command": "gobby",
            "args": ["mcp-server"],
        }

    # Write updated settings
    try:
        with open(settings_path, "w") as f:
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

    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        result["error"] = f"Failed to read {settings_path}: {e}"
        return result

    # Check if server exists
    if "mcpServers" not in settings or server_name not in settings["mcpServers"]:
        result["success"] = True
        return result

    # Create backup
    timestamp = int(time.time())
    backup_path = settings_path.parent / f"{settings_path.name}.{timestamp}.backup"
    try:
        copy2(settings_path, backup_path)
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
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
    except OSError as e:
        result["error"] = f"Failed to write {settings_path}: {e}"
        return result

    result["success"] = True
    result["removed"] = True
    return result


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
    import re

    result: dict[str, Any] = {
        "success": False,
        "added": False,
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
        result["success"] = True
        result["already_configured"] = True
        return result

    # Create backup if file exists
    if config_path.exists():
        timestamp = int(time.time())
        backup_path = config_path.with_suffix(f".toml.{timestamp}.backup")
        try:
            backup_path.write_text(existing, encoding="utf-8")
            result["backup_path"] = str(backup_path)
        except OSError as e:
            result["error"] = f"Failed to create backup: {e}"
            return result

    # Add MCP server config
    # Use 'uv run gobby' since most users won't have gobby installed globally
    mcp_config = f"""
[mcp_servers.{server_name}]
command = "uv"
args = ["run", "gobby", "mcp-server"]
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


def remove_mcp_server_toml(config_path: Path, server_name: str = "gobby") -> dict[str, Any]:
    """Remove Gobby MCP server from a TOML config file.

    Uses tomllib (stdlib) for reading and tomli_w for writing to properly
    handle TOML syntax including multi-line strings.

    Args:
        config_path: Path to the config.toml file
        server_name: Name of the MCP server entry to remove

    Returns:
        Dict with 'success', 'removed', 'backup_path', and 'error' keys
    """
    import tomllib

    import tomli_w

    result: dict[str, Any] = {
        "success": False,
        "removed": False,
        "backup_path": None,
        "error": None,
    }

    if not config_path.exists():
        result["success"] = True
        return result

    # Read existing TOML file
    try:
        existing_text = config_path.read_text(encoding="utf-8")
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
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
    timestamp = int(time.time())
    backup_path = config_path.with_suffix(f".toml.{timestamp}.backup")
    try:
        backup_path.write_text(existing_text, encoding="utf-8")
        result["backup_path"] = str(backup_path)
    except OSError as e:
        result["error"] = f"Failed to create backup: {e}"
        return result

    # Remove the server from config
    del mcp_servers[server_name]

    # Clean up empty mcp_servers section
    if not mcp_servers:
        del config["mcp_servers"]
    else:
        config["mcp_servers"] = mcp_servers

    # Write updated config using tomli_w
    try:
        with open(config_path, "wb") as f:
            tomli_w.dump(config, f, multiline_strings=True)
    except OSError as e:
        result["error"] = f"Failed to write {config_path}: {e}"
        return result

    result["success"] = True
    result["removed"] = True
    return result


# Default external MCP servers to install
DEFAULT_MCP_SERVERS: list[dict[str, Any]] = [
    {
        "name": "github",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "$secret:github_personal_access_token"},
        "description": "GitHub API integration for issues, PRs, repos, and code search",
    },
    {
        "name": "linear",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "mcp-linear"],
        "env": {"LINEAR_API_KEY": "$secret:linear_api_key"},
        "description": "Linear issue tracking integration",
    },
    {
        "name": "brave-search",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@brave/brave-search-mcp-server"],
        "env": {"BRAVE_API_KEY": "$secret:brave_api_key"},
        "description": "Brave Search API for web search, local search, and news",
    },
    {
        "name": "context7",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
        "optional_secret_args": {"context7_api_key": ["--api-key"]},
        "description": "Context7 library documentation lookup (set context7_api_key secret for private repos)",
    },
]


def install_default_mcp_servers() -> dict[str, Any]:
    """Install default external MCP servers to ~/.gobby/.mcp.json.

    Adds default MCP servers (GitHub, Linear, Brave Search, context7) if not
    already configured. Also syncs to the database so the daemon proxy can
    serve them. These servers pull API keys from environment variables.

    Returns:
        Dict with 'success', 'servers_added', 'servers_skipped', and 'error' keys
    """
    result: dict[str, Any] = {
        "success": False,
        "servers_added": [],
        "servers_skipped": [],
        "error": None,
    }

    mcp_config_path = Path("~/.gobby/.mcp.json").expanduser()

    # Ensure parent directory exists
    mcp_config_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing config or create empty
    existing_config: dict[str, Any] = {"servers": []}
    if mcp_config_path.exists():
        try:
            with open(mcp_config_path) as f:
                content = f.read()
                if content.strip():
                    existing_config = json.loads(content)
                    if "servers" not in existing_config:
                        existing_config["servers"] = []
        except (json.JSONDecodeError, OSError) as e:
            result["error"] = f"Failed to read MCP config: {e}"
            return result

    # Get existing server names
    existing_names = {s.get("name") for s in existing_config["servers"]}

    # Repair misconfigured servers: reconcile transport/command/args/env with defaults
    servers_repaired: list[str] = []
    default_by_name = {s["name"]: s for s in DEFAULT_MCP_SERVERS}
    for existing_server in existing_config["servers"]:
        name = existing_server.get("name")
        default = default_by_name.get(name) if name else None
        if not default:
            continue
        repaired = False
        # Check if transport diverged from the canonical default
        if existing_server.get("transport") != default["transport"]:
            existing_server["transport"] = default["transport"]
            existing_server["command"] = default.get("command")
            existing_server["args"] = list(default.get("args") or [])
            existing_server.pop("url", None)
            repaired = True
        # Overwrite env with canonical $secret: references
        if "env" in default and existing_server.get("env") != default["env"]:
            existing_server["env"] = dict(default["env"])
            repaired = True
        if repaired:
            servers_repaired.append(name)
    result["servers_repaired"] = servers_repaired

    # Resolve optional_secret_args via secret store (lazy init)
    secret_store = None
    secret_store_init_failed = False

    # Add default servers if not already present
    for server in DEFAULT_MCP_SERVERS:
        if server["name"] in existing_names:
            result["servers_skipped"].append(server["name"])
        else:
            # Build args list, adding optional secret-dependent args
            args = list(server.get("args") or [])
            optional_secret_args = server.get("optional_secret_args", {})
            for secret_name, extra_args in optional_secret_args.items():
                if secret_store is None and not secret_store_init_failed:
                    try:
                        from gobby.storage.database import LocalDatabase
                        from gobby.storage.secrets import SecretStore

                        secret_store = SecretStore(LocalDatabase())
                    except (ImportError, OSError):
                        secret_store_init_failed = True
                if secret_store is not None and secret_store.exists(secret_name):
                    secret_value = secret_store.get(secret_name)
                    if secret_value:
                        args.extend(extra_args + [secret_value])

            existing_config["servers"].append(
                {
                    "name": server["name"],
                    "enabled": True,
                    "transport": server["transport"],
                    "command": server.get("command"),
                    "args": args if args else None,
                    "env": server.get("env"),
                    "description": server.get("description"),
                }
            )
            result["servers_added"].append(server["name"])

    # Write updated config if any servers were added or repaired
    if result["servers_added"] or servers_repaired:
        try:
            with open(mcp_config_path, "w") as f:
                json.dump(existing_config, f, indent=2)
            # Set restrictive permissions
            mcp_config_path.chmod(0o600)
        except OSError as e:
            result["error"] = f"Failed to write MCP config: {e}"
            return result

    if servers_repaired:
        logger.info(f"Repaired MCP server configs: {', '.join(servers_repaired)}")

    # Sync .mcp.json to database so the daemon proxy can serve them
    try:
        from gobby.storage.database import LocalDatabase
        from gobby.storage.mcp import LocalMCPManager
        from gobby.storage.projects import GLOBAL_PROJECT_ID

        db = LocalDatabase()
        mcp_db = LocalMCPManager(db)
        imported = mcp_db.import_from_mcp_json(mcp_config_path, project_id=GLOBAL_PROJECT_ID)
        if imported:
            logger.info(f"Synced {imported} MCP servers to database")
    except Exception as e:
        logger.warning(f"Failed to sync MCP servers to database: {e}")

    result["success"] = True
    return result

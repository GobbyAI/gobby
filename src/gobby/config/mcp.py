"""
MCP Configuration Manager for persistent server configuration.

Manages MCP server configurations stored in ~/.gobby/mcp-servers.json,
providing thread-safe read/write operations with validation.

The file lived at ``~/.gobby/.mcp.json`` historically; that filename collides
with Claude Code's project-level ``.mcp.json`` schema (Claude Code's
``/doctor`` walks the tree and tries to parse it with a different schema).
``migrate_legacy_mcp_config()`` moves the old file in place on first access.
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.manager import MCPServerConfig
from gobby.storage.projects import GLOBAL_PROJECT_ID

__all__ = [
    "DEFAULT_MCP_CONFIG_PATH",
    "LEGACY_MCP_CONFIG_PATH",
    "MCPConfigManager",
    "MCPServerConfig",
    "migrate_legacy_mcp_config",
]

logger = logging.getLogger(__name__)

DEFAULT_MCP_CONFIG_PATH = "~/.gobby/mcp-servers.json"
LEGACY_MCP_CONFIG_PATH = "~/.gobby/.mcp.json"


def migrate_legacy_mcp_config(
    new_path: str | Path = DEFAULT_MCP_CONFIG_PATH,
    legacy_path: str | Path = LEGACY_MCP_CONFIG_PATH,
) -> bool:
    """Rename the legacy ``~/.gobby/.mcp.json`` to the new location.

    Idempotent: returns ``True`` only when the legacy file existed and the new
    file did not. If both exist we leave them alone (the new file wins) and
    log a warning so the operator can clean up. If neither exists the call is
    a no-op.
    """
    legacy = Path(legacy_path).expanduser()
    new = Path(new_path).expanduser()

    if not legacy.exists():
        return False

    if new.exists():
        logger.warning(
            "Both legacy MCP config %s and new %s exist; leaving legacy file in "
            "place. Delete %s after confirming %s has the desired contents.",
            legacy,
            new,
            legacy,
            new,
        )
        return False

    try:
        new.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Failed to create MCP config directory %s: %s", new.parent, exc)
        return False

    try:
        legacy.replace(new)
    except OSError as replace_exc:
        logger.warning(
            "Failed to move legacy MCP config %s -> %s with replace (%s); "
            "attempting copy/unlink fallback.",
            legacy,
            new,
            replace_exc,
        )
        try:
            shutil.copy2(legacy, new)
        except OSError as copy_exc:
            logger.warning(
                "Failed to copy legacy MCP config %s -> %s: %s",
                legacy,
                new,
                copy_exc,
            )
            return False
        try:
            legacy.unlink()
        except OSError as unlink_exc:
            logger.warning(
                "Copied legacy MCP config %s -> %s but failed to remove legacy file: %s. "
                "Using the copied config; remove the legacy file manually after verifying it.",
                legacy,
                new,
                unlink_exc,
            )

    try:
        new.chmod(0o600)
    except OSError as chmod_exc:
        logger.warning(
            "Migrated MCP config %s -> %s but failed to set permissions on %s: %s. "
            "Verify and fix permissions on %s before using it.",
            legacy,
            new,
            new,
            chmod_exc,
            new,
        )
        return False

    logger.info("Migrated MCP config %s -> %s", legacy, new)
    return True


class MCPConfigManager:
    """
    Manages persistent MCP server configurations in ~/.gobby/mcp-servers.json.

    Provides thread-safe operations for reading, writing, adding, and removing
    MCP server configurations with automatic validation and file locking.

    Configuration file format:
        {
            "servers": [
                {
                    "name": "context7",
                    "enabled": true,
                    "transport": "stdio",
                    "command": "uvx",
                    "args": ["context7-mcp"],
                    "env": null
                },
                {
                    "name": "supabase",
                    "enabled": true,
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@supabase/mcp-server@latest"],
                    "env": null
                }
            ]
        }
    """

    def __init__(self, config_path: str | None = None):
        """
        Initialize MCP configuration manager.

        Args:
            config_path: Path to MCP config file (default: ~/.gobby/mcp-servers.json)
        """
        if config_path is None:
            migrate_legacy_mcp_config()
            config_path = DEFAULT_MCP_CONFIG_PATH

        self.config_path = Path(config_path).expanduser()

        # Ensure parent directory exists
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize config file if it doesn't exist
        if not self.config_path.exists():
            self._write_config({"servers": []})

    def _read_config(self) -> dict[str, Any]:
        """
        Read MCP configuration from file.

        Returns:
            Configuration dictionary

        Raises:
            ValueError: If config file is invalid JSON
        """
        try:
            with open(self.config_path) as f:
                content = f.read()

            if not content.strip():
                return {"servers": []}

            config = json.loads(content)

            # Validate structure
            if not isinstance(config, dict):
                raise ValueError("Config must be a JSON object")

            if "servers" not in config:
                config["servers"] = []

            if not isinstance(config["servers"], list):
                raise ValueError("'servers' must be an array")

            return config

        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in MCP config file: {e}") from e

    def _write_config(self, config: dict[str, Any]) -> None:
        """
        Write MCP configuration to file.

        Args:
            config: Configuration dictionary to write

        Raises:
            OSError: If file operations fail
        """
        # Write to temporary file first for atomic write
        temp_path = self.config_path.with_suffix(".tmp")

        try:
            with open(temp_path, "w") as f:
                json.dump(config, f, indent=2)

            # Atomic rename
            temp_path.replace(self.config_path)

            # Set restrictive permissions (owner read/write only)
            self.config_path.chmod(0o600)

        except Exception as e:
            # Clean up temp file if it exists
            if temp_path.exists():
                temp_path.unlink()
            raise OSError(f"Failed to write MCP config: {e}") from e

    def load_servers(self) -> list[MCPServerConfig]:
        """
        Load MCP server configurations from file.

        Returns:
            List of MCPServerConfig objects

        Raises:
            ValueError: If configuration is invalid
        """
        config = self._read_config()
        servers = []

        for server_dict in config.get("servers", []):
            try:
                # Validate required fields
                if "name" not in server_dict:
                    logger.warning(f"Skipping MCP server config without 'name': {server_dict}")
                    continue

                # Create MCPServerConfig with defaults for optional fields
                # File-based configs use GLOBAL_PROJECT_ID since they're system-wide
                server_config = MCPServerConfig(
                    name=server_dict["name"],
                    project_id=server_dict.get("project_id", GLOBAL_PROJECT_ID),
                    enabled=server_dict.get("enabled", True),
                    transport=server_dict.get("transport", "http"),
                    url=server_dict.get("url"),
                    headers=server_dict.get("headers"),
                    command=server_dict.get("command"),
                    args=server_dict.get("args"),
                    env=server_dict.get("env"),
                    requires_oauth=server_dict.get("requires_oauth", False),
                    oauth_provider=server_dict.get("oauth_provider"),
                    tools=server_dict.get("tools"),
                    description=server_dict.get("description"),
                )

                # Validate configuration
                server_config.validate()

                servers.append(server_config)

            except Exception as e:
                logger.error(
                    f"Failed to load MCP server config '{server_dict.get('name', 'unknown')}': {e}"
                )
                continue

        return servers

    def save_servers(self, servers: list[MCPServerConfig]) -> None:
        """
        Save MCP server configurations to file.

        Args:
            servers: List of MCPServerConfig objects to save

        Raises:
            ValueError: If server configuration is invalid
            OSError: If file operations fail
        """
        # Convert MCPServerConfig objects to dictionaries
        server_dicts = []

        for server in servers:
            # Validate before saving
            server.validate()

            server_dict = {
                "name": server.name,
                "enabled": server.enabled,
                "transport": server.transport,
            }

            # Add transport-specific fields
            if server.transport in ("http", "websocket", "sse"):
                server_dict["url"] = server.url
                if server.headers:
                    server_dict["headers"] = server.headers
                if server.requires_oauth:
                    server_dict["requires_oauth"] = server.requires_oauth
                    if server.oauth_provider:
                        server_dict["oauth_provider"] = server.oauth_provider

            elif server.transport == "stdio":
                server_dict["command"] = server.command
                if server.args:
                    server_dict["args"] = server.args
                if server.env:
                    server_dict["env"] = server.env

            # Add tool metadata if available
            if server.tools:
                server_dict["tools"] = server.tools

            # Add description if available
            if server.description:
                server_dict["description"] = server.description

            server_dicts.append(server_dict)

        # Write to file
        config = {"servers": server_dicts}
        self._write_config(config)

    def add_server(self, server: MCPServerConfig) -> None:
        """
        Add MCP server configuration.

        Args:
            server: MCPServerConfig to add

        Raises:
            ValueError: If server with same name already exists or config is invalid
            OSError: If file operations fail
        """
        # Validate server config
        server.validate()

        # Load existing servers
        servers = self.load_servers()

        # Check for duplicate name
        if any(s.name == server.name for s in servers):
            raise ValueError(f"MCP server '{server.name}' already exists in configuration")

        # Add new server
        servers.append(server)

        # Save updated configuration
        self.save_servers(servers)

    def remove_server(self, name: str) -> None:
        """
        Remove MCP server configuration by name.

        Args:
            name: Server name to remove

        Raises:
            ValueError: If server not found
            OSError: If file operations fail
        """
        # Load existing servers
        servers = self.load_servers()

        # Find and remove server
        original_count = len(servers)
        servers = [s for s in servers if s.name != name]

        if len(servers) == original_count:
            raise ValueError(f"MCP server '{name}' not found in configuration")

        # Save updated configuration
        self.save_servers(servers)

    def update_server(self, server: MCPServerConfig) -> None:
        """
        Update existing MCP server configuration.

        Args:
            server: MCPServerConfig with updated values

        Raises:
            ValueError: If server not found or config is invalid
            OSError: If file operations fail
        """
        # Validate server config
        server.validate()

        # Load existing servers
        servers = self.load_servers()

        # Find and update server
        found = False
        for i, s in enumerate(servers):
            if s.name == server.name:
                servers[i] = server
                found = True
                break

        if not found:
            raise ValueError(f"MCP server '{server.name}' not found in configuration")

        # Save updated configuration
        self.save_servers(servers)

    def get_server(self, name: str) -> MCPServerConfig | None:
        """
        Get MCP server configuration by name.

        Args:
            name: Server name to find

        Returns:
            MCPServerConfig if found, None otherwise
        """
        servers = self.load_servers()

        for server in servers:
            if server.name == name:
                return server

        return None

    def list_servers(self) -> list[str]:
        """
        Get list of configured MCP server names.

        Returns:
            List of server names
        """
        servers = self.load_servers()
        return [s.name for s in servers]

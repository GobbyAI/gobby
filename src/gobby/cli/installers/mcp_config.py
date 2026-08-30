"""
MCP server configuration functions for Gobby installers.

Extracted from shared.py as part of Strangler Fig decomposition (Wave 2).
Handles configuring/removing MCP server entries in JSON and TOML config files.
"""

import logging
import time
from shutil import copy2

from gobby.config.mcp import DEFAULT_MCP_CONFIG_PATH

from .mcp_config_defaults import DEFAULT_MCP_SERVERS, install_default_mcp_servers
from .mcp_config_json import (
    configure_mcp_server_json,
    configure_project_mcp_server,
    remove_mcp_server_json,
    remove_project_mcp_server,
)
from .mcp_config_toml import (
    configure_mcp_server_toml,
    remove_mcp_server_toml,
    strip_mcp_tool_overrides_toml,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MCP_CONFIG_PATH",
    "DEFAULT_MCP_SERVERS",
    "configure_mcp_server_json",
    "configure_mcp_server_toml",
    "configure_project_mcp_server",
    "copy2",
    "install_default_mcp_servers",
    "remove_mcp_server_json",
    "remove_mcp_server_toml",
    "remove_project_mcp_server",
    "strip_mcp_tool_overrides_toml",
    "time",
]

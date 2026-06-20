"""
MCP server configuration functions for Gobby installers.

Extracted from shared.py as part of Strangler Fig decomposition (Wave 2).
Handles configuring/removing MCP server entries in JSON and TOML config files.
"""

import logging
import time
from pathlib import Path
from shutil import copy2

from gobby.config.mcp import DEFAULT_MCP_CONFIG_PATH
from gobby.mcp_proxy.bundled import DEFAULT_EXTERNAL_MCP_SERVERS

from .mcp_config_defaults import install_default_mcp_servers
from .mcp_config_json import (
    configure_mcp_server_json,
    configure_project_mcp_server,
    remove_mcp_server_json,
    remove_project_mcp_server,
)
from .mcp_config_shared import (
    _CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC,
    _GOBBY_MCP_ARGS,
    _GOBBY_MCP_COMMAND,
    _command_basename,
    _is_current_gobby_mcp_server_config,
    _is_repairable_stale_gobby_mcp_server_config,
    _needs_codex_gobby_mcp_tool_timeout,
    _remove_toml_table_block,
    _repair_stale_gobby_mcp_server_toml,
    _toml_string_list,
    _trailing_blank_or_comment_lines,
)
from .mcp_config_toml import (
    configure_mcp_server_toml,
    remove_mcp_server_toml,
    strip_mcp_tool_overrides_toml,
)

logger = logging.getLogger(__name__)

# Default external MCP servers to install.
DEFAULT_MCP_SERVERS = DEFAULT_EXTERNAL_MCP_SERVERS

__all__ = [
    "DEFAULT_MCP_CONFIG_PATH",
    "DEFAULT_MCP_SERVERS",
    "Path",
    "_CODEX_GOBBY_MCP_TOOL_TIMEOUT_SEC",
    "_GOBBY_MCP_ARGS",
    "_GOBBY_MCP_COMMAND",
    "_command_basename",
    "_is_current_gobby_mcp_server_config",
    "_is_repairable_stale_gobby_mcp_server_config",
    "_needs_codex_gobby_mcp_tool_timeout",
    "_remove_toml_table_block",
    "_repair_stale_gobby_mcp_server_toml",
    "_toml_string_list",
    "_trailing_blank_or_comment_lines",
    "configure_mcp_server_json",
    "configure_mcp_server_toml",
    "configure_project_mcp_server",
    "copy2",
    "install_default_mcp_servers",
    "logger",
    "remove_mcp_server_json",
    "remove_mcp_server_toml",
    "remove_project_mcp_server",
    "strip_mcp_tool_overrides_toml",
    "time",
]

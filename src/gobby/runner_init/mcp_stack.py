"""MCP database manager and proxy stack initialisation."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from gobby.config.logging import RUNTIME_LOG_FILENAME, resolved_log_path
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.metrics import ToolMetricsManager
from gobby.mcp_proxy.metrics_events import MetricsEventStore
from gobby.mcp_proxy.templates import MCPServerTemplate, expand_template
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.mcp_models import MCPServer
from gobby.storage.mcp_templates import MCPServerTemplateRow
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secrets import SecretStore

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

logger = logging.getLogger(__name__)

__all__ = [
    "LocalMCPManager",
    "MCPClientManager",
    "MetricsEventStore",
    "ToolMetricsManager",
    "init_mcp_db_manager",
    "init_mcp_stack",
    "resolved_log_path",
]


def init_mcp_db_manager(runner: GobbyRunner) -> None:
    """Construct MCP storage and refresh template instances."""
    runner.mcp_db_manager = LocalMCPManager(runner.database)
    manager = runner.mcp_db_manager
    try:
        result = manager.refresh_template_instances(
            lambda template, server: _expand_instance(manager, template, server)
        )
    except Exception:
        logger.exception("error refreshing MCP template instances")
        result = {"refreshed": 0, "errors": {}}
    refreshed = result.get("refreshed", 0)
    if refreshed:
        logger.info("Refreshed %s MCP template instances", refreshed)
    errors = result.get("errors") or {}
    if not isinstance(errors, dict):
        return
    for error in errors.values():
        _log_stale_instance(error)


def init_mcp_stack(runner: GobbyRunner) -> None:
    """Initialise MCP storage, metrics, and the client manager."""
    init_mcp_db_manager(runner)
    runner.metrics_event_store = MetricsEventStore(runner.database)
    runner.metrics_manager = ToolMetricsManager(
        runner.database, event_store=runner.metrics_event_store
    )
    runner.mcp_proxy = MCPClientManager(
        mcp_db_manager=runner.mcp_db_manager,
        metrics_manager=runner.metrics_manager,
        stdio_errlog_path=str(
            resolved_log_path(runner.startup_config.logging, RUNTIME_LOG_FILENAME)
        ),
    )


def _expand_instance(
    manager: LocalMCPManager,
    template_row: MCPServerTemplateRow,
    server: MCPServer,
) -> Mapping[str, Any]:
    template = MCPServerTemplate.from_definition(template_row.definition)
    secret_store = SecretStore(manager.db)
    raw_values = server.template_values or {}
    values = {
        str(key): value if isinstance(value, str) else str(value)
        for key, value in raw_values.items()
    }
    expanded = expand_template(
        template,
        name=server.name,
        project_id=str(server.project_id),
        values=values,
        description=server.description,
        secret_exists=lambda name: secret_store.exists(name, project_id=str(server.project_id)),
    )
    config = expanded.config
    return {
        "transport": config.transport,
        "url": config.url,
        "command": config.command,
        "args": config.args,
        "env": config.env,
        "headers": config.headers,
        "connect_timeout": config.connect_timeout,
        "runtime_hook": config.runtime_hook,
    }


def _log_stale_instance(error: object) -> None:
    if not isinstance(error, dict):
        logger.warning("Template instance failed to expand: %s", error)
        return
    name = str(error.get("name", "<unknown>"))
    scope = str(error.get("project_id", ""))
    message = str(error.get("error", ""))
    global_flag = " --global" if scope == GLOBAL_PROJECT_ID else ""
    logger.warning(
        "Template instance %s (%s) failed to expand: %s. "
        "Fix with `gobby mcp-proxy add-server --template %s --set ...` or "
        "`gobby secrets set <name>%s`",
        name,
        scope,
        message,
        name,
        global_flag,
    )

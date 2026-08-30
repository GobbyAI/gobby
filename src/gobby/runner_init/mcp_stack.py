"""MCP database manager and proxy stack initialisation."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from gobby.config.embedding_keys import MCP_SCOPED_PAYLOAD_VERSION_KEY
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

SCOPED_PAYLOAD_VERSION_KEY = MCP_SCOPED_PAYLOAD_VERSION_KEY
SCOPED_PAYLOAD_VERSION = 1

__all__ = [
    "LocalMCPManager",
    "MCPClientManager",
    "MetricsEventStore",
    "ToolMetricsManager",
    "SCOPED_PAYLOAD_VERSION",
    "SCOPED_PAYLOAD_VERSION_KEY",
    "init_mcp_db_manager",
    "init_mcp_stack",
    "maybe_backfill_scoped_tool_embeddings",
    "resolved_log_path",
    "schedule_scoped_embedding_backfill",
]


def _store_get(config_store: Any, key: str) -> Any:
    getter = getattr(config_store, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except TypeError:
            return getter(key, None)
    reader = getattr(config_store, "read_snapshot", None)
    if callable(reader):
        snapshot = reader()
        overrides = dict(getattr(snapshot, "overrides", {}) or {})
        values = dict(getattr(snapshot, "values", {}) or {})
        return overrides.get(key, values.get(key))
    return None


def _store_set(config_store: Any, key: str, value: Any) -> None:
    setter = getattr(config_store, "set", None)
    if callable(setter):
        setter(key, value)
        return
    # The marker key is registry-RESTRICTED, so the public patch surface
    # rejects it; daemon-owned writes go through patch_internal.
    patch_internal = getattr(getattr(config_store, "mutations", None), "patch_internal", None)
    if callable(patch_internal):
        from gobby.storage.config_mutations import ConfigPatch

        snapshot = config_store.read_snapshot()
        patch_internal(
            expected_revision=snapshot.revision,
            patch=ConfigPatch(values={key: value}),
            source="daemon",
        )


async def maybe_backfill_scoped_tool_embeddings(
    search: Any,
    mcp_manager: Any,
    *,
    config_store: Any,
    vector_store: Any | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Rewrite legacy tool embeddings once after the scoped-payload change."""
    raw = _store_get(config_store, SCOPED_PAYLOAD_VERSION_KEY)
    try:
        current = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        current = 0
    if current >= SCOPED_PAYLOAD_VERSION:
        return {"rewritten": False, "embedded": 0}
    if vector_store is not None and getattr(search, "_vector_store", None) is None:
        search._vector_store = vector_store
    try:
        stats = await search.embed_all_tools(project_id or GLOBAL_PROJECT_ID, mcp_manager)
    except Exception:
        logger.exception("Scoped tool embedding backfill failed; will retry on next start")
        raise
    _store_set(config_store, SCOPED_PAYLOAD_VERSION_KEY, SCOPED_PAYLOAD_VERSION)
    embedded = int(stats.get("embedded", 0)) if isinstance(stats, dict) else 0
    return {"rewritten": True, "embedded": embedded, **(stats if isinstance(stats, dict) else {})}


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
    schedule_scoped_embedding_backfill(
        getattr(runner, "semantic_search", None),
        runner.mcp_proxy,
        config_store=getattr(runner, "config_store", None),
    )


_BACKFILL_TASKS: set[Any] = set()


def schedule_scoped_embedding_backfill(
    search: Any,
    mcp_manager: Any,
    *,
    config_store: Any,
) -> None:
    """Run the one-shot scoped-payload backfill, inline or as a loop task."""
    if search is None or config_store is None:
        return
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(
                maybe_backfill_scoped_tool_embeddings(
                    search,
                    mcp_manager,
                    config_store=config_store,
                )
            )
        except Exception:
            logger.exception("Scoped tool embedding backfill failed")
    else:

        async def _run() -> None:
            try:
                await maybe_backfill_scoped_tool_embeddings(
                    search,
                    mcp_manager,
                    config_store=config_store,
                )
            except Exception:
                logger.exception("Scoped tool embedding backfill failed")

        task = asyncio.create_task(_run())
        _BACKFILL_TASKS.add(task)
        task.add_done_callback(_BACKFILL_TASKS.discard)


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

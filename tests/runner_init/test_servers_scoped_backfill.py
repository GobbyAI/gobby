"""init_servers schedules the scoped tool-embedding backfill with the internal manager.

The services path no longer schedules the backfill: init_stateful_services runs before
init_servers builds the internal manager, and the internal-manager guard in
schedule_scoped_embedding_backfill made that call a no-op. This module covers the live
call site in runner_init/servers.py, which must pass internal_manager, and uses the real
guard and the real marker store on the isolated test hub.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.runner import GobbyRunner
from gobby.runner_init.mcp_stack import (
    SCOPED_PAYLOAD_VERSION,
    SCOPED_PAYLOAD_VERSION_KEY,
    _store_get,
)
from gobby.runner_init.servers import init_servers
from gobby.storage.config_store import ConfigStore
from gobby.storage.projects import GLOBAL_PROJECT_ID
from tests.config_runtime_helpers import static_runtime_capture

pytestmark = pytest.mark.unit


class _RunnerStub(SimpleNamespace):
    """A SimpleNamespace subclass: init_servers takes a weak reference to the runner."""


def _runner(database: Any, mcp_proxy: Any) -> GobbyRunner:
    """A runner stub carrying every attribute init_servers reads, with websocket disabled."""
    config = DaemonConfig(websocket={"enabled": False})
    return cast(
        GobbyRunner,
        _RunnerStub(
            startup_config=config,
            bootstrap_config=BootstrapConfig(),
            machine_id="machine-1",
            codex_client=None,
            text_generation_service=None,
            database=database,
            db_executor=None,
            worktree_delete_executor=None,
            coverage_executor=None,
            database_concurrency=None,
            database_watchdog=None,
            session_manager=None,
            task_manager=object(),
            span_storage=None,
            managed_credential_manager=None,
            memory_backup_manager=None,
            memory_manager=None,
            memory_dream_coordinator=None,
            feedback_review_service=None,
            llm_service=None,
            vector_store=None,
            mcp_proxy=mcp_proxy,
            mcp_db_manager=None,
            metrics_manager=None,
            agent_runner=None,
            message_processor=None,
            worktree_storage=None,
            clone_storage=None,
            git_manager=None,
            project_id="project-1",
            pipeline_executor=None,
            workflow_loader=None,
            pipeline_execution_manager=None,
            completion_registry=None,
            wake_dispatcher=SimpleNamespace(set_web_chat_session_registry=MagicMock()),
            agent_lifecycle_monitor=None,
            attention_manager=None,
            detection_registry=None,
            communications_manager=None,
            code_indexer=None,
            cron_storage=None,
            cron_scheduler=None,
            system_automation_loop=None,
            skill_manager=None,
            hub_manager=None,
            config_store=None,
            config_runtime=SimpleNamespace(
                ready=False,
                capture=static_runtime_capture(config),
                register_revision_publisher=MagicMock(),
            ),
            prompt_manager=None,
            tool_chat_service=None,
            _dev_mode=False,
        ),
    )


def _http_server(search: Any, internal_manager: Any, *, tools_handler: bool) -> MagicMock:
    """The HTTPServer seam: a tools handler carries the semantic search, as in production."""
    server = MagicMock(name="HTTPServer")
    server._tools_handler = SimpleNamespace(_semantic_search=search) if tools_handler else None
    server._internal_manager = internal_manager
    return server


def _run_init_servers(runner: GobbyRunner, http_server: MagicMock) -> None:
    """Drive the real init_servers. Only the HTTP server and unrelated subsystems are faked.

    The backfill scheduler and its guard are not patched, so the call site under test
    decides whether the embedding run happens.
    """
    with (
        patch("gobby.runner_init.servers.HTTPServer", return_value=http_server),
        patch("gobby.runner_init.servers.WebChatRuntimeManager"),
        patch("gobby.runner_init.servers.ModelMetadataCoverageAuditor"),
        patch("gobby.runner_init.servers.CapabilityRefreshCoordinator"),
        patch("gobby.runner_init.servers.ProviderCapacityService.create_default"),
        patch("gobby.runner_init.servers.LocalContextStore"),
        patch("gobby.runner_init.servers.LocalContextService"),
        patch("gobby.runner_init.servers.configured_local_routes", return_value=()),
        patch("gobby.runner_init.servers.set_app_context"),
        patch(
            "gobby.adapters.codex_impl.app_server_adapter.CodexAdapter.is_codex_available",
            return_value=False,
        ),
    ):
        init_servers(runner)


def test_init_servers_schedules_backfill_with_internal_manager(temp_db: Any) -> None:
    search = SimpleNamespace(embed_all_tools=AsyncMock(return_value={"embedded": 3}))
    internal_manager = object()
    mcp_proxy = MagicMock()

    _run_init_servers(
        _runner(temp_db, mcp_proxy),
        _http_server(search, internal_manager, tools_handler=True),
    )

    search.embed_all_tools.assert_awaited_once_with(
        GLOBAL_PROJECT_ID, mcp_proxy, internal_manager=internal_manager
    )
    assert _store_get(ConfigStore(temp_db), SCOPED_PAYLOAD_VERSION_KEY) == SCOPED_PAYLOAD_VERSION


def test_init_servers_backfill_runs_once_across_restarts(temp_db: Any) -> None:
    search = SimpleNamespace(embed_all_tools=AsyncMock(return_value={"embedded": 3}))
    internal_manager = object()

    for _ in range(2):
        _run_init_servers(
            _runner(temp_db, MagicMock()),
            _http_server(search, internal_manager, tools_handler=True),
        )

    search.embed_all_tools.assert_awaited_once()
    assert _store_get(ConfigStore(temp_db), SCOPED_PAYLOAD_VERSION_KEY) == SCOPED_PAYLOAD_VERSION


def test_init_servers_skips_backfill_without_tools_handler(temp_db: Any) -> None:
    """Without MCP subsystems there is no tools handler, so nothing is scheduled or marked."""
    _run_init_servers(_runner(temp_db, MagicMock()), _http_server(None, None, tools_handler=False))

    assert _store_get(ConfigStore(temp_db), SCOPED_PAYLOAD_VERSION_KEY) is None

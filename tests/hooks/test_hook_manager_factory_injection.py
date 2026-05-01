"""Tests for HookManager dependency injection wiring."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.hooks.factory import HookManagerFactory
from gobby.hooks.hook_manager import HookManager
from gobby.storage.database import LocalDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


def _mock_components(database: object, session_manager: object) -> SimpleNamespace:
    return SimpleNamespace(
        config=MagicMock(),
        database=database,
        daemon_client=MagicMock(),
        transcript_processor=MagicMock(),
        session_task_manager=MagicMock(),
        memory_storage=MagicMock(),
        task_manager=MagicMock(),
        agent_run_manager=MagicMock(),
        worktree_manager=MagicMock(),
        stop_registry=MagicMock(),
        progress_tracker=MagicMock(),
        stuck_detector=MagicMock(),
        memory_manager=MagicMock(),
        workflow_loader=MagicMock(),
        skill_manager=MagicMock(),
        pipeline_executor=MagicMock(),
        workflow_handler=MagicMock(),
        webhook_dispatcher=MagicMock(),
        session_manager=session_manager,
        session_coordinator=MagicMock(),
        health_monitor=MagicMock(),
        hook_assembler=MagicMock(),
        event_handlers=MagicMock(),
    )


def test_hook_manager_forwards_injected_database_and_session_manager() -> None:
    """HookManager should pass daemon-owned storage handles into the factory."""
    database = MagicMock()
    session_manager = MagicMock()
    components = _mock_components(database, session_manager)

    with (
        patch("gobby.hooks.hook_manager.HookManagerFactory.create") as create,
        patch("gobby.hooks.hook_manager.asyncio.get_running_loop", side_effect=RuntimeError),
        patch("gobby.hooks.event_enrichment.EventEnricher"),
        patch("gobby.hooks.session_lookup.SessionLookupService"),
        patch("gobby.storage.inter_session_messages.InterSessionMessageManager"),
    ):
        create.return_value = components
        manager = HookManager(
            daemon_host="localhost",
            daemon_port=60887,
            database=database,
            session_manager=session_manager,
            log_file="/tmp/test-hook-manager.log",
        )

    assert create.call_args.kwargs["database"] is database
    assert create.call_args.kwargs["session_manager"] is session_manager
    assert manager._database is database
    assert manager._session_manager is session_manager


def test_factory_create_reuses_injected_session_manager(
    temp_db: LocalDatabase,
    session_manager: SessionManager,
    default_config: DaemonConfig,
) -> None:
    """Factory-created components should share the injected SessionManager."""
    workflow_components = SimpleNamespace(
        loader=MagicMock(),
        template_engine=MagicMock(),
        skill_manager=MagicMock(),
        pipeline_executor=None,
        handler=MagicMock(),
    )
    autonomous_components = SimpleNamespace(
        stop_registry=MagicMock(),
        progress_tracker=MagicMock(),
        stuck_detector=MagicMock(),
    )

    with (
        patch.object(HookManagerFactory, "_create_database") as create_database,
        patch.object(
            HookManagerFactory,
            "_create_autonomous",
            return_value=autonomous_components,
        ),
        patch.object(HookManagerFactory, "_create_memory", return_value=MagicMock()),
        patch.object(
            HookManagerFactory,
            "_create_workflow_engine",
            return_value=workflow_components,
        ),
        patch.object(HookManagerFactory, "_create_webhooks", return_value=MagicMock()),
    ):
        components = HookManagerFactory.create(
            daemon_host="localhost",
            daemon_port=60887,
            llm_service=None,
            config=default_config,
            hook_logger=logging.getLogger("test"),
            loop=None,
            broadcaster=None,
            tool_proxy_getter=None,
            message_processor=None,
            memory_sync_manager=None,
            task_sync_manager=None,
            agent_runner=None,
            completion_registry=None,
            get_machine_id=lambda: "machine-1",
            resolve_project_id=lambda _project_id, _cwd: "project-1",
            session_manager=session_manager,
        )

    create_database.assert_not_called()
    assert components.database is temp_db
    assert components.session_manager is session_manager
    assert components.session_coordinator._session_manager is session_manager
    assert components.event_handlers._session_manager is session_manager
    assert components.session_task_manager.db is temp_db
    assert components.task_manager.db is temp_db

"""Tests for HookManager dependency injection wiring."""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.hooks.factory import HookManagerFactory
from gobby.hooks.hook_manager import HookManager
from gobby.storage.hub.protocol import HubDatabase
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
        session_end_auto_link_worker=MagicMock(),
        health_monitor=MagicMock(),
        event_handlers=MagicMock(),
    )


def test_hook_manager_forwards_injected_database_and_session_manager() -> None:
    """HookManager should pass daemon-owned storage handles into the factory."""
    database = MagicMock()
    session_manager = MagicMock()
    config_runtime = MagicMock()
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
            config_runtime=config_runtime,
        )

    assert create.call_args.kwargs["database"] is database
    assert create.call_args.kwargs["session_manager"] is session_manager
    assert create.call_args.kwargs["config_runtime"] is config_runtime
    assert manager._database is database
    assert manager._session_manager is session_manager


def test_factory_config_resolution_prefers_active_runtime_snapshot() -> None:
    startup = MagicMock()
    active = MagicMock()
    runtime = MagicMock(snapshot=SimpleNamespace(active=active))

    assert HookManagerFactory._resolve_config(startup, runtime) is active


def test_factory_config_resolution_falls_back_to_supplied_config_on_snapshot_failure() -> None:
    class BrokenRuntime:
        @property
        def snapshot(self) -> object:
            raise RuntimeError("ConfigRuntime has not started")

    startup = MagicMock()
    assert HookManagerFactory._resolve_config(startup, cast(Any, BrokenRuntime())) is startup


def test_hook_manager_shutdown_leaves_injected_database_open() -> None:
    """HookManager shutdown should leave daemon-owned storage handles open."""
    database = MagicMock()
    session_manager = MagicMock()
    components = _mock_components(database, session_manager)
    components.webhook_dispatcher.close = AsyncMock()

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
        )

    manager.shutdown()

    database.close.assert_not_called()
    assert database.close.call_count == 0
    assert not database.close.called


def test_factory_create_reuses_injected_session_manager(
    temp_db: HubDatabase,
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
    config_runtime = MagicMock()

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
        ) as create_workflow_engine,
        patch.object(HookManagerFactory, "_create_webhooks", return_value=MagicMock()),
    ):
        components = HookManagerFactory.create(
            daemon_host="localhost",
            daemon_port=60887,
            llm_service=None,
            llm_service_resolver=lambda: None,
            config=default_config,
            hook_logger=logging.getLogger("test"),
            loop=None,
            broadcaster=None,
            tool_proxy_getter=None,
            message_processor_resolver=lambda: None,
            agent_runner=None,
            completion_registry=None,
            get_machine_id=lambda: "21000000-0000-4000-8000-000000000001",
            resolve_project_id=lambda _project_id, _cwd: "project-1",
            session_manager=session_manager,
            config_runtime=config_runtime,
        )

    create_database.assert_not_called()
    assert components.database is temp_db
    assert components.session_manager is session_manager
    assert cast(object, components.session_coordinator._session_manager) is session_manager
    assert cast(object, components.event_handlers._session_manager) is session_manager
    assert components.session_task_manager.db is temp_db
    assert components.task_manager.db is temp_db
    bound_args = inspect.signature(HookManagerFactory._create_workflow_engine).bind(
        *create_workflow_engine.call_args.args,
        **create_workflow_engine.call_args.kwargs,
    )
    assert bound_args.arguments["config_runtime"] is config_runtime


def _patched_subsystems() -> tuple[SimpleNamespace, SimpleNamespace]:
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
    return workflow_components, autonomous_components


def _create_kwargs(session_manager: SessionManager, default_config: DaemonConfig) -> dict[str, Any]:
    return {
        "daemon_host": "localhost",
        "daemon_port": 60887,
        "llm_service": None,
        "llm_service_resolver": lambda: None,
        "config": default_config,
        "hook_logger": logging.getLogger("test"),
        "loop": None,
        "broadcaster": None,
        "tool_proxy_getter": None,
        "message_processor_resolver": lambda: None,
        "agent_runner": None,
        "completion_registry": None,
        "get_machine_id": lambda: "21000000-0000-4000-8000-000000000001",
        "resolve_project_id": lambda _project_id, _cwd: "project-1",
        "session_manager": session_manager,
        "config_runtime": MagicMock(),
    }


def test_factory_create_uses_injected_memory_manager(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    default_config: DaemonConfig,
) -> None:
    """The daemon's fully-wired MemoryManager must be shared, not rebuilt (#17491)."""
    workflow_components, autonomous_components = _patched_subsystems()
    shared_memory_manager = MagicMock()
    daemon_loop = MagicMock()

    with (
        patch.object(HookManagerFactory, "_create_autonomous", return_value=autonomous_components),
        patch.object(HookManagerFactory, "_create_memory") as create_memory,
        patch.object(
            HookManagerFactory, "_create_workflow_engine", return_value=workflow_components
        ) as create_workflow_engine,
        patch.object(HookManagerFactory, "_create_webhooks", return_value=MagicMock()),
    ):
        kwargs = _create_kwargs(session_manager, default_config)
        kwargs["loop"] = daemon_loop
        components = HookManagerFactory.create(
            memory_manager=shared_memory_manager,
            **kwargs,
        )

    create_memory.assert_not_called()
    create_workflow_engine.assert_called_once()
    bound_args = inspect.signature(HookManagerFactory._create_workflow_engine).bind(
        *create_workflow_engine.call_args.args,
        **create_workflow_engine.call_args.kwargs,
    )
    assert bound_args.arguments["memory_manager"] is shared_memory_manager
    assert bound_args.arguments["daemon_loop"] is daemon_loop
    assert components.memory_manager is shared_memory_manager


def test_factory_create_memory_fallback_threads_llm_service(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    default_config: DaemonConfig,
) -> None:
    """Without an injected manager, the fallback receives the factory's llm_service."""
    workflow_components, autonomous_components = _patched_subsystems()
    fallback_manager = MagicMock()
    llm_service = MagicMock()

    with (
        patch.object(HookManagerFactory, "_create_autonomous", return_value=autonomous_components),
        patch.object(
            HookManagerFactory, "_create_memory", return_value=fallback_manager
        ) as create_memory,
        patch.object(
            HookManagerFactory, "_create_workflow_engine", return_value=workflow_components
        ) as create_workflow_engine,
        patch.object(HookManagerFactory, "_create_webhooks", return_value=MagicMock()),
    ):
        kwargs = _create_kwargs(session_manager, default_config)
        kwargs["llm_service"] = llm_service
        components = HookManagerFactory.create(**kwargs)

    create_memory.assert_called_once_with(
        temp_db,
        default_config,
        llm_service,
        kwargs["llm_service_resolver"],
    )
    create_workflow_engine.assert_called_once()
    bound_args = inspect.signature(HookManagerFactory._create_workflow_engine).bind(
        *create_workflow_engine.call_args.args,
        **create_workflow_engine.call_args.kwargs,
    )
    assert bound_args.arguments["memory_manager"] is fallback_manager
    assert components.memory_manager is fallback_manager


def test_hook_manager_forwards_injected_memory_manager() -> None:
    """HookManager should pass the daemon's MemoryManager into the factory."""
    database = MagicMock()
    session_manager = MagicMock()
    memory_manager = MagicMock()
    components = _mock_components(database, session_manager)
    components.memory_manager = memory_manager

    with (
        patch("gobby.hooks.hook_manager.HookManagerFactory.create") as create,
        patch("gobby.hooks.hook_manager.asyncio.get_running_loop", side_effect=RuntimeError),
        patch("gobby.hooks.event_enrichment.EventEnricher"),
        patch("gobby.hooks.session_lookup.SessionLookupService"),
        patch("gobby.storage.inter_session_messages.InterSessionMessageManager"),
    ):
        create.return_value = components
        HookManager(
            daemon_host="localhost",
            daemon_port=60887,
            database=database,
            session_manager=session_manager,
            memory_manager=memory_manager,
        )

    assert create.call_count == 1
    assert create.call_args.kwargs["memory_manager"] is memory_manager
    assert create.call_args.kwargs["database"] is database
    assert create.call_args.kwargs["session_manager"] is session_manager


def test_factory_create_supplies_registry_and_does_not_build_claude_parser(
    session_manager: SessionManager,
    default_config: DaemonConfig,
) -> None:
    from gobby.sessions.transcripts import get_parser
    from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
    from gobby.sessions.transcripts.droid import DroidTranscriptParser

    workflow_components, autonomous_components = _patched_subsystems()
    claude_cls = MagicMock(wraps=ClaudeTranscriptParser)
    droid_cls = MagicMock(wraps=DroidTranscriptParser)

    # Route through the shared registry seam: a parser built by module
    # reference would bypass PARSER_REGISTRY and leave these spies inert.
    with (
        patch.dict(
            "gobby.sessions.transcripts.PARSER_REGISTRY",
            {"claude": claude_cls, "droid": droid_cls},
            clear=True,
        ),
        patch.object(HookManagerFactory, "_create_autonomous", return_value=autonomous_components),
        patch.object(HookManagerFactory, "_create_memory", return_value=MagicMock()),
        patch.object(
            HookManagerFactory, "_create_workflow_engine", return_value=workflow_components
        ),
        patch.object(HookManagerFactory, "_create_webhooks", return_value=MagicMock()),
    ):
        components = HookManagerFactory.create(**_create_kwargs(session_manager, default_config))
        assert components.transcript_processor is get_parser
        claude_cls.assert_not_called()
        parser = components.transcript_processor("droid", session_id="s1")

    droid_cls.assert_called_once_with(session_id="s1", transcript_path=None)
    claude_cls.assert_not_called()
    assert isinstance(parser, DroidTranscriptParser)
    assert not isinstance(parser, ClaudeTranscriptParser)

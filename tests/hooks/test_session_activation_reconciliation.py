"""Session activation reconciliation tests."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.hooks.session_activation import (
    _ACTIVE_RULE_NAMES_CACHE,
    _ACTIVE_RULE_NAMES_CACHE_MAX_ENTRIES,
    _ACTIVE_RULE_NAMES_CACHE_TTL_SECONDS,
    MARKER_COMPLETED,
    MARKER_HASH,
    MARKER_VERSION,
    SESSION_ACTIVATION_CONTRACT_HASH,
    SESSION_ACTIVATION_CONTRACT_VERSION,
    _agent_run_from_row,
    clear_active_rule_names_cache,
    reconcile_session_activation,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.database import LocalDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import WorkflowInstance
from gobby.workflows.git_utils import DirtyFiles
from gobby.workflows.state_manager import SessionVariableManager, WorkflowInstanceManager

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_rule_cache() -> Iterator[None]:
    clear_active_rule_names_cache()
    yield
    clear_active_rule_names_cache()


@pytest.fixture
def db(temp_db: LocalDatabase) -> LocalDatabase:
    return temp_db


@pytest.fixture
def project_id(db: LocalDatabase, tmp_path) -> str:
    project = LocalProjectManager(db).create(name="activation-test", repo_path=str(tmp_path))
    return project.id


@pytest.fixture
def session_manager(db: LocalDatabase) -> SessionManager:
    return SessionManager(db)


@pytest.fixture
def handlers(session_manager: SessionManager) -> EventHandlers:
    return EventHandlers(session_manager=session_manager)  # type: ignore[arg-type]


def _event(event_type: HookEventType, session_id: str, tmp_path) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="external-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"cwd": str(tmp_path)},
        metadata={"_platform_session_id": session_id},
    )


def _register_session(
    session_manager: SessionManager,
    project_id: str,
    tmp_path,
    *,
    external_id: str = "external-1",
    agent_depth: int = 0,
) -> str:
    return session_manager.register_session(
        external_id=external_id,
        machine_id="machine-1",
        source="claude",
        project_id=project_id,
        project_path=str(tmp_path),
        agent_depth=agent_depth,
    )


def _variables(db: LocalDatabase, session_id: str) -> dict:
    return SessionVariableManager(db).get_variables(session_id)


def _create_worker_agent(db: LocalDatabase) -> None:
    manager = LocalWorkflowDefinitionManager(db)
    manager.create(
        name="worker",
        workflow_type="agent",
        source="custom",
        definition_json=json.dumps(
            {
                "name": "worker",
                "role": "Worker",
                "steps": [{"name": "claim"}, {"name": "implement"}],
                "step_variables": {"ticket": "14475"},
            }
        ),
    )
    manager.create(
        name="worker-steps",
        workflow_type="workflow",
        source="agent",
        enabled=False,
        definition_json=json.dumps(
            {
                "name": "worker-steps",
                "type": "step",
                "steps": [{"name": "claim"}, {"name": "implement"}],
                "variables": {"ticket": "14475"},
            }
        ),
    )


def _create_parent_and_child(
    db: LocalDatabase,
    session_manager: SessionManager,
    project_id: str,
    tmp_path,
) -> tuple[str, str]:
    parent_id = _register_session(
        session_manager,
        project_id,
        tmp_path,
        external_id="parent-external",
    )
    child_id = _register_session(
        session_manager,
        project_id,
        tmp_path,
        external_id="child-external",
        agent_depth=1,
    )
    LocalAgentRunManager(db).create(
        parent_session_id=parent_id,
        provider="claude",
        prompt="do the work",
        workflow_name="worker-flow",
        agent_name="worker",
        child_session_id=child_id,
        run_id="run-worker",
    )
    return parent_id, child_id


def test_marker_creation_on_session_start(
    db: LocalDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path,
) -> None:
    session_id = _register_session(session_manager, project_id, tmp_path)
    result = reconcile_session_activation(
        _event(HookEventType.SESSION_START, session_id, tmp_path),
        handlers,
    )

    variables = _variables(db, session_id)
    assert result.changed is True
    assert variables[MARKER_COMPLETED] is True
    assert variables[MARKER_VERSION] == SESSION_ACTIVATION_CONTRACT_VERSION
    assert variables[MARKER_HASH] == SESSION_ACTIVATION_CONTRACT_HASH


def test_before_agent_fast_noop_when_current(
    db: LocalDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path,
) -> None:
    session_id = _register_session(session_manager, project_id, tmp_path)
    event = _event(HookEventType.BEFORE_AGENT, session_id, tmp_path)
    reconcile_session_activation(event, handlers)

    handlers._activate_default_agent = MagicMock(side_effect=AssertionError("slow path"))
    result = reconcile_session_activation(event, handlers)

    assert result.changed is False
    assert result.reason == "current"


def test_missing_agent_type_restored_before_rules(
    db: LocalDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path,
) -> None:
    session_id = _register_session(session_manager, project_id, tmp_path)
    SessionVariableManager(db).merge_variables(
        session_id,
        {
            MARKER_COMPLETED: True,
            MARKER_VERSION: SESSION_ACTIVATION_CONTRACT_VERSION,
            MARKER_HASH: SESSION_ACTIVATION_CONTRACT_HASH,
            "baseline_dirty_files": [],
            "session_edited_files": [],
        },
    )

    result = reconcile_session_activation(
        _event(HookEventType.BEFORE_AGENT, session_id, tmp_path),
        handlers,
    )

    variables = _variables(db, session_id)
    assert "_agent_type" in result.missing
    assert variables["_agent_type"] == "default"
    assert "is_spawned_agent" in variables


def test_reconciliation_refreshes_stale_active_rule_names(
    db: LocalDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path,
) -> None:
    """Refresh stale active rule names from the installed agent definition.

    Reconciliation must replace the session's cached `_active_rule_names` when the
    agent selector now resolves a different rule set, so stale rules stop firing
    after bundled/custom workflow changes.
    """
    session_id = _register_session(session_manager, project_id, tmp_path)
    manager = LocalWorkflowDefinitionManager(db)
    manager.create(
        name="default",
        workflow_type="agent",
        source="custom",
        definition_json=json.dumps(
            {
                "name": "default",
                "workflows": {"rule_selectors": {"include": ["tag:default"], "exclude": []}},
            }
        ),
    )
    manager.create(
        name="new-default-rule",
        workflow_type="rule",
        source="custom",
        tags=["default"],
        definition_json=json.dumps(
            {
                "event": "before_tool",
                "effects": [{"type": "set_variable", "variable": "matched", "value": True}],
            }
        ),
    )
    SessionVariableManager(db).merge_variables(
        session_id,
        {
            MARKER_COMPLETED: True,
            MARKER_VERSION: SESSION_ACTIVATION_CONTRACT_VERSION,
            MARKER_HASH: SESSION_ACTIVATION_CONTRACT_HASH,
            "_agent_type": "default",
            "_active_rule_names": ["stale-rule"],
            "_active_skill_names": None,
            "_skill_format": None,
            "_agent_blocked_tools": [],
            "_agent_blocked_mcp_tools": [],
            "is_spawned_agent": False,
            "baseline_dirty_files": [],
            "session_edited_files": [],
        },
    )

    result = reconcile_session_activation(
        _event(HookEventType.BEFORE_AGENT, session_id, tmp_path),
        handlers,
    )

    variables = _variables(db, session_id)
    assert result.changed is True
    assert variables["_active_rule_names"] == ["new-default-rule"]


def test_reconciliation_caches_active_rule_names_for_same_agent_and_project(
    db: LocalDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _register_session(session_manager, project_id, tmp_path)
    manager = LocalWorkflowDefinitionManager(db)
    manager.create(
        name="default",
        workflow_type="agent",
        source="custom",
        definition_json=json.dumps(
            {
                "name": "default",
                "workflows": {"rule_selectors": {"include": ["tag:default"], "exclude": []}},
            }
        ),
    )
    manager.create(
        name="cached-rule",
        workflow_type="rule",
        source="custom",
        tags=["default"],
        definition_json=json.dumps(
            {
                "event": "before_tool",
                "effects": [{"type": "set_variable", "variable": "matched", "value": True}],
            }
        ),
    )
    SessionVariableManager(db).merge_variables(
        session_id,
        {
            MARKER_COMPLETED: True,
            MARKER_VERSION: SESSION_ACTIVATION_CONTRACT_VERSION,
            MARKER_HASH: SESSION_ACTIVATION_CONTRACT_HASH,
            "_agent_type": "default",
            "_active_rule_names": ["stale-rule"],
            "_active_skill_names": None,
            "_skill_format": None,
            "_agent_blocked_tools": [],
            "_agent_blocked_mcp_tools": [],
            "is_spawned_agent": False,
            "baseline_dirty_files": [],
            "session_edited_files": [],
        },
    )

    list_all_calls = 0
    original_list_all = LocalWorkflowDefinitionManager.list_all

    def counted_list_all(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal list_all_calls
        list_all_calls += 1
        return original_list_all(self, *args, **kwargs)

    monkeypatch.setattr(LocalWorkflowDefinitionManager, "list_all", counted_list_all)

    event = _event(HookEventType.BEFORE_AGENT, session_id, tmp_path)
    reconcile_session_activation(event, handlers)
    reconcile_session_activation(event, handlers)

    assert _variables(db, session_id)["_active_rule_names"] == ["cached-rule"]
    assert list_all_calls == 1


def test_reconciliation_invalidates_active_rule_cache_after_definition_mutation(
    db: LocalDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path,
) -> None:
    session_id = _register_session(session_manager, project_id, tmp_path)
    manager = LocalWorkflowDefinitionManager(db)
    manager.create(
        name="default",
        workflow_type="agent",
        source="custom",
        definition_json=json.dumps(
            {
                "name": "default",
                "workflows": {"rule_selectors": {"include": ["tag:default"], "exclude": []}},
            }
        ),
    )
    manager.create(
        name="cached-rule",
        workflow_type="rule",
        source="custom",
        tags=["default"],
        definition_json=json.dumps(
            {
                "event": "before_tool",
                "effects": [{"type": "set_variable", "variable": "matched", "value": True}],
            }
        ),
    )
    SessionVariableManager(db).merge_variables(
        session_id,
        {
            MARKER_COMPLETED: True,
            MARKER_VERSION: SESSION_ACTIVATION_CONTRACT_VERSION,
            MARKER_HASH: SESSION_ACTIVATION_CONTRACT_HASH,
            "_agent_type": "default",
            "_active_rule_names": ["stale-rule"],
            "_active_skill_names": None,
            "_skill_format": None,
            "_agent_blocked_tools": [],
            "_agent_blocked_mcp_tools": [],
            "is_spawned_agent": False,
            "baseline_dirty_files": [],
            "session_edited_files": [],
        },
    )

    event = _event(HookEventType.BEFORE_AGENT, session_id, tmp_path)
    reconcile_session_activation(event, handlers)
    assert _variables(db, session_id)["_active_rule_names"] == ["cached-rule"]

    manager.create(
        name="new-rule",
        workflow_type="rule",
        source="custom",
        tags=["default"],
        definition_json=json.dumps(
            {
                "event": "before_tool",
                "effects": [{"type": "set_variable", "variable": "new_matched", "value": True}],
            }
        ),
    )

    reconcile_session_activation(event, handlers)

    assert _variables(db, session_id)["_active_rule_names"] == ["cached-rule", "new-rule"]


def test_active_rule_names_cache_evicts_oldest_entries(
    db: LocalDatabase,
    project_id: str,
) -> None:
    manager = LocalWorkflowDefinitionManager(db)
    manager.create(
        name="new-agent",
        workflow_type="agent",
        source="custom",
        definition_json=json.dumps(
            {
                "name": "new-agent",
                "workflows": {"rule_selectors": {"include": [], "exclude": []}},
            }
        ),
    )
    now = time.monotonic()
    for index in range(_ACTIVE_RULE_NAMES_CACHE_MAX_ENTRIES + 1):
        _ACTIVE_RULE_NAMES_CACHE[(f"agent-{index}", project_id)] = (
            now - 1 + (index * 0.001),
            {f"rule-{index}"},
        )

    from gobby.hooks.session_activation import _resolve_active_rule_names

    assert _resolve_active_rule_names(db, "new-agent", project_id) == set()
    assert len(_ACTIVE_RULE_NAMES_CACHE) == _ACTIVE_RULE_NAMES_CACHE_MAX_ENTRIES
    assert ("agent-0", project_id) not in _ACTIVE_RULE_NAMES_CACHE


def test_active_rule_names_cache_purges_expired_entries(
    db: LocalDatabase,
    project_id: str,
) -> None:
    manager = LocalWorkflowDefinitionManager(db)
    manager.create(
        name="new-agent",
        workflow_type="agent",
        source="custom",
        definition_json=json.dumps(
            {
                "name": "new-agent",
                "workflows": {"rule_selectors": {"include": [], "exclude": []}},
            }
        ),
    )
    now = time.monotonic()
    _ACTIVE_RULE_NAMES_CACHE[("stale-agent", project_id)] = (
        now - _ACTIVE_RULE_NAMES_CACHE_TTL_SECONDS - 1,
        {"stale-rule"},
    )
    _ACTIVE_RULE_NAMES_CACHE[("fresh-agent", project_id)] = (now, {"fresh-rule"})

    from gobby.hooks.session_activation import _resolve_active_rule_names

    assert _resolve_active_rule_names(db, "new-agent", project_id) == set()
    assert ("stale-agent", project_id) not in _ACTIVE_RULE_NAMES_CACHE
    assert ("fresh-agent", project_id) in _ACTIVE_RULE_NAMES_CACHE


def test_spawned_step_agent_restores_workflow_variable_and_instance(
    db: LocalDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path,
) -> None:
    _create_worker_agent(db)
    _, child_id = _create_parent_and_child(db, session_manager, project_id, tmp_path)

    result = reconcile_session_activation(
        _event(HookEventType.BEFORE_AGENT, child_id, tmp_path),
        handlers,
    )

    variables = _variables(db, child_id)
    instance = WorkflowInstanceManager(db).get_instance(child_id, "worker-steps")
    assert "worker-steps" in result.missing
    assert variables["_step_workflow_name"] == "worker-steps"
    assert variables["step_workflow_complete"] is False
    assert instance is not None
    assert instance.current_step == "claim"


def test_existing_step_workflow_current_step_is_preserved(
    db: LocalDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path,
) -> None:
    _create_worker_agent(db)
    _, child_id = _create_parent_and_child(db, session_manager, project_id, tmp_path)
    WorkflowInstanceManager(db).save_instance(
        WorkflowInstance(
            id="instance-worker",
            session_id=child_id,
            workflow_name="worker-steps",
            enabled=True,
            priority=10,
            current_step="implement",
            variables={"ticket": "14475"},
        )
    )
    SessionVariableManager(db).merge_variables(
        child_id,
        {
            "_agent_type": "worker",
            "_step_workflow_name": "worker-steps",
            "is_spawned_agent": True,
        },
    )

    reconcile_session_activation(_event(HookEventType.BEFORE_AGENT, child_id, tmp_path), handlers)

    instance = WorkflowInstanceManager(db).get_instance(child_id, "worker-steps")
    assert instance is not None
    assert instance.current_step == "implement"


def test_baseline_dirty_initializes_once_and_preserves_session_edits(
    db: LocalDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _register_session(session_manager, project_id, tmp_path)
    SessionVariableManager(db).merge_variables(session_id, {"session_edited_files": ["kept.py"]})

    monkeypatch.setattr(
        "gobby.workflows.git_utils.get_dirty_files_categorized",
        lambda _path: DirtyFiles(tracked={"dirty.py"}, untracked={"new.py"}),
    )

    reconcile_session_activation(_event(HookEventType.BEFORE_AGENT, session_id, tmp_path), handlers)

    variables = _variables(db, session_id)
    assert variables["baseline_dirty_files"] == ["dirty.py", "new.py"]
    assert variables["session_edited_files"] == ["kept.py"]


def test_terminal_pickup_metadata_backfills_from_agent_runs(
    db: LocalDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path,
) -> None:
    _create_worker_agent(db)
    _, child_id = _create_parent_and_child(db, session_manager, project_id, tmp_path)

    result = reconcile_session_activation(
        _event(HookEventType.BEFORE_AGENT, child_id, tmp_path),
        handlers,
    )

    session = session_manager.get(child_id)
    assert session is not None
    assert "terminal_pickup_metadata" in result.missing
    assert session.agent_run_id == "run-worker"
    assert session.workflow_name == "worker-flow"
    assert session.original_prompt == "do the work"


def test_agent_run_from_row_returns_none_when_required_keys_are_missing() -> None:
    assert _agent_run_from_row({"id": "run-worker"}) is None


def test_agent_run_from_row_rejects_malformed_required_fields() -> None:
    valid_nullable_fields = {"workflow_name": None, "agent_name": None, "prompt": None}

    assert _agent_run_from_row({"id": "", **valid_nullable_fields}) is None
    assert _agent_run_from_row({"id": 123, **valid_nullable_fields}) is None
    assert (
        _agent_run_from_row(
            {
                "id": "run-worker",
                "workflow_name": "worker-flow",
                "agent_name": 123,
                "prompt": "do the work",
            }
        )
        is None
    )


def test_agent_run_from_row_returns_recovery_for_valid_row() -> None:
    recovery = _agent_run_from_row(
        {
            "id": "run-worker",
            "workflow_name": "worker-flow",
            "agent_name": "worker",
            "prompt": "do the work",
        }
    )

    assert recovery is not None
    assert recovery.id == "run-worker"
    assert recovery.workflow_name == "worker-flow"
    assert recovery.agent_name == "worker"
    assert recovery.prompt == "do the work"


def test_hook_manager_reconciles_before_before_agent_rules() -> None:
    components = MagicMock()
    for name in (
        "config",
        "database",
        "daemon_client",
        "transcript_processor",
        "session_task_manager",
        "memory_storage",
        "message_manager",
        "task_manager",
        "agent_run_manager",
        "worktree_manager",
        "stop_registry",
        "progress_tracker",
        "stuck_detector",
        "memory_manager",
        "workflow_loader",
        "skill_manager",
        "pipeline_executor",
        "workflow_handler",
        "webhook_dispatcher",
        "session_manager",
        "session_coordinator",
        "health_monitor",
        "hook_assembler",
        "event_handlers",
    ):
        setattr(components, name, MagicMock())
    components.webhook_dispatcher.config.enabled = False
    components.health_monitor.get_cached_status.return_value = (True, None, "running", None)
    components.health_monitor.check_now.return_value = True
    components.webhook_dispatcher.get_blocking_decision.return_value = (None, None)

    call_order: list[str] = []
    handler = MagicMock(
        side_effect=lambda event: call_order.append("handler") or HookResponse(decision="allow")
    )
    components.event_handlers.get_handler.return_value = handler
    components.workflow_handler.handle.side_effect = lambda event: call_order.append(
        "rules"
    ) or HookResponse(decision="allow")

    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="external-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"prompt": "hello"},
        metadata={"_platform_session_id": "session-1"},
    )

    with (
        patch("gobby.hooks.hook_manager.HookManagerFactory.create", return_value=components),
        patch("gobby.hooks.hook_manager.asyncio.get_running_loop", side_effect=RuntimeError),
        patch("gobby.hooks.hook_manager.record_session_activity"),
        patch(
            "gobby.hooks.hook_manager.resolve_hook_project_context",
            return_value=SimpleNamespace(skipped=False, reason=None, project_id="proj"),
        ),
        patch(
            "gobby.hooks.hook_manager.reconcile_session_activation",
            side_effect=lambda event, handler, logger=None: call_order.append("reconcile"),
        ),
        patch("gobby.hooks.event_enrichment.EventEnricher"),
        patch("gobby.hooks.session_lookup.SessionLookupService"),
        patch("gobby.storage.inter_session_messages.InterSessionMessageManager"),
    ):
        manager = HookManager(log_file="/tmp/test-hook-manager.log")
        manager._session_lookup.resolve.return_value = None
        manager._enricher.enrich = MagicMock()
        manager._handle_internal(event)

    assert call_order == ["reconcile", "rules", "handler"]

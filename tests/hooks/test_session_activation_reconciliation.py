"""Session activation reconciliation tests."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
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
    _AGENT_KEYS,
    MARKER_COMPLETED,
    MARKER_HASH,
    MARKER_VERSION,
    SESSION_ACTIVATION_CONTRACT_HASH,
    SESSION_ACTIVATION_CONTRACT_VERSION,
    _agent_has_step_workflow,
    _agent_run_from_row,
    _AgentRunRecovery,
    _ensure_step_instance,
    _missing_step_state,
    clear_active_rule_names_cache,
    reconcile_session_activation,
)
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import TERMINAL_SESSION_STATUSES, SessionManager
from gobby.workflows.definitions import (
    RuleDefinitionBody,
    RuleEffect,
    RuleTriggerEvent,
)
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.git_utils import DirtyFiles
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.workflows.step_instance_fixtures import make_step_instance

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture(autouse=True)
def clear_rule_cache() -> Iterator[None]:
    clear_active_rule_names_cache()
    yield
    clear_active_rule_names_cache()


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


@pytest.fixture
def project_id(db: HubDatabase, tmp_path: Path) -> str:
    project = LocalProjectManager(db).create(name="activation-test", repo_path=str(tmp_path))
    return project.id


@pytest.fixture
def session_manager(db: HubDatabase) -> SessionManager:
    return SessionManager(db)


@pytest.fixture
def handlers(session_manager: SessionManager) -> EventHandlers:
    return EventHandlers(session_manager=session_manager)  # type: ignore[arg-type]


def test_agent_has_step_workflow_uses_typed_resolver(db: HubDatabase) -> None:
    session = SimpleNamespace(project_id="project-id")
    agent = SimpleNamespace(step_workflow=object())
    with patch("gobby.workflows.agent_resolver.resolve_agent", return_value=agent) as resolve:
        exists = _agent_has_step_workflow(
            db,
            {"_agent_type": "reviewer"},
            session,
            None,
        )

    assert exists is True
    resolve.assert_called_once_with("reviewer", db, project_id="project-id")


def _event(event_type: HookEventType, session_id: str, tmp_path: Path) -> HookEvent:
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
    tmp_path: Path,
    *,
    external_id: str = "external-1",
    agent_depth: int = 0,
    parent_session_id: str | None = None,
) -> str:
    return session_manager.register_session(
        external_id=external_id,
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=project_id,
        project_path=str(tmp_path),
        agent_depth=agent_depth,
        parent_session_id=parent_session_id,
    )


def _variables(db: HubDatabase, session_id: str) -> dict[str, Any]:
    return SessionVariableManager(db).get_variables(session_id)


def _create_worker_agent(db: HubDatabase) -> None:
    AgentDefinitionManager(db).upsert_with_steps(
        "worker",
        {
            "name": "worker",
            "prompts": {"agent": "Work the assigned task."},
            "blocked_tools": ["Bash"],
            "blocked_mcp_tools": ["gobby-tasks.close_task"],
            "workflows": {"rule_selectors": {"include": ["tag:worker"], "exclude": []}},
        },
        {
            "variables": {"ticket": "14475"},
            "steps": [{"name": "claim"}, {"name": "implement"}],
        },
        source="custom",
    )


def _create_parent_and_child(
    db: HubDatabase,
    session_manager: SessionManager,
    project_id: str,
    tmp_path: Path,
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
        run_id="90e65240-4167-55c2-84df-72f933aee3a8",
    )
    return parent_id, child_id


def test_marker_creation_on_session_start(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
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
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
) -> None:
    session_id = _register_session(session_manager, project_id, tmp_path)
    event = _event(HookEventType.BEFORE_AGENT, session_id, tmp_path)
    reconcile_session_activation(event, handlers)

    with patch.object(
        handlers,
        "_activate_default_agent",
        side_effect=AssertionError("slow path"),
    ):
        result = reconcile_session_activation(event, handlers)

    assert result.changed is False
    assert result.reason == "current"


@pytest.mark.parametrize("status", sorted(TERMINAL_SESSION_STATUSES))
def test_terminal_session_status_skips_reconciliation(
    status: str,
    tmp_path: Path,
) -> None:
    session = SimpleNamespace(id="terminal-session", status=status)
    db_access = MagicMock(side_effect=AssertionError("terminal session touched database"))

    class TerminalSessionManager:
        def get(self, _session_id: str) -> Any:
            return session

        @property
        def db(self) -> Any:
            db_access()
            raise AssertionError("terminal session touched database")

    handler = SimpleNamespace(_session_manager=TerminalSessionManager())
    event = _event(HookEventType.BEFORE_AGENT, "terminal-session", tmp_path)

    with (
        patch("gobby.workflows.state_manager.SessionVariableManager") as variable_manager,
        patch("gobby.hooks.session_activation._recover_agent_run") as recover_agent_run,
        patch("gobby.hooks.session_activation._activate_agent") as activate_agent,
    ):
        result = reconcile_session_activation(event, handler)

    assert result.changed is False
    assert result.reason == f"session_status_terminal:{status}"
    db_access.assert_not_called()
    variable_manager.assert_not_called()
    recover_agent_run.assert_not_called()
    activate_agent.assert_not_called()


def test_expired_session_resumes_across_turn_start_and_end(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
) -> None:
    session_id = _register_session(session_manager, project_id, tmp_path)
    session_manager.update_status(session_id, "expired")
    session_manager.mark_transcript_processed(session_id)
    before_agent = _event(HookEventType.BEFORE_AGENT, session_id, tmp_path)
    before_agent.data["prompt"] = "resume work"

    handlers.handle_before_agent(before_agent)

    active = session_manager.get(session_id)
    assert active is not None
    assert active.status == "active"
    transcript_row = db.fetchone(
        "SELECT transcript_processed FROM sessions WHERE id = %s",
        (session_id,),
    )
    assert transcript_row is not None
    assert transcript_row["transcript_processed"] is False

    handlers.handle_after_agent(_event(HookEventType.AFTER_AGENT, session_id, tmp_path))

    paused = session_manager.get(session_id)
    assert paused is not None
    assert paused.status == "paused"


def test_missing_agent_type_restored_before_rules(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
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


@pytest.mark.parametrize("failure_mode", ["exception", "missing_definition"])
def test_spawned_agent_activation_failure_retries_without_default_markers(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure_mode: str,
) -> None:
    _create_worker_agent(db)
    RuleDefinitionManager(db).create(
        name="worker-only-rule",
        source="custom",
        tags=["worker"],
        definition_json=json.dumps(
            {
                "event": "before_tool",
                "effects": [{"type": "set_variable", "variable": "worker_rule", "value": True}],
            }
        ),
    )
    _, child_id = _create_parent_and_child(db, session_manager, project_id, tmp_path)
    event = _event(HookEventType.BEFORE_AGENT, child_id, tmp_path)
    real_activate = handlers._activate_default_agent
    attempts = 0

    def flaky_activate(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if failure_mode == "exception":
                raise RuntimeError("temporary activation failure")
            return None
        return real_activate(*args, **kwargs)

    with (
        patch.object(handlers, "_activate_default_agent", side_effect=flaky_activate),
        caplog.at_level("WARNING"),
    ):
        first = reconcile_session_activation(event, handlers)
        failed_variables = _variables(db, child_id)

        assert first.changed is True
        assert attempts == 1
        assert not set(_AGENT_KEYS).intersection(failed_variables)
        assert MARKER_COMPLETED not in failed_variables
        assert MARKER_VERSION not in failed_variables
        assert MARKER_HASH not in failed_variables
        assert "worker" in caplog.text

        second = reconcile_session_activation(event, handlers)

    variables = _variables(db, child_id)
    assert second.changed is True
    assert attempts == 2
    assert variables["_agent_type"] == "worker"
    assert variables["_active_rule_names"] == ["worker-only-rule"]
    assert variables["_agent_blocked_tools"] == ["Bash"]
    assert variables["_agent_blocked_mcp_tools"] == ["gobby-tasks.close_task"]
    assert variables[MARKER_COMPLETED] is True


def test_reconciliation_refreshes_stale_active_rule_names(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
) -> None:
    """Refresh stale active rule names from the installed agent definition.

    Reconciliation must replace the session's cached `_active_rule_names` when the
    agent selector now resolves a different rule set, so stale rules stop firing
    after bundled/custom workflow changes.
    """
    session_id = _register_session(session_manager, project_id, tmp_path)
    manager = RuleDefinitionManager(db)
    AgentDefinitionManager(db).create(
        name="default",
        source="custom",
        definition_json=json.dumps(
            {
                "name": "default",
                "prompts": {"agent": "Run the assigned task."},
                "workflows": {"rule_selectors": {"include": ["tag:default"], "exclude": []}},
            }
        ),
    )
    manager.create(
        name="new-default-rule",
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
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _register_session(session_manager, project_id, tmp_path)
    manager = RuleDefinitionManager(db)
    AgentDefinitionManager(db).create(
        name="default",
        source="custom",
        definition_json=json.dumps(
            {
                "name": "default",
                "prompts": {"agent": "Run the assigned task."},
                "workflows": {"rule_selectors": {"include": ["tag:default"], "exclude": []}},
            }
        ),
    )
    manager.create(
        name="cached-rule",
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
    original_list_all = RuleDefinitionManager.list_all

    def counted_list_all(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal list_all_calls
        list_all_calls += 1
        return original_list_all(self, *args, **kwargs)

    monkeypatch.setattr(RuleDefinitionManager, "list_all", counted_list_all)

    event = _event(HookEventType.BEFORE_AGENT, session_id, tmp_path)
    reconcile_session_activation(event, handlers)
    reconcile_session_activation(event, handlers)

    assert _variables(db, session_id)["_active_rule_names"] == ["cached-rule"]
    assert list_all_calls == 1


def test_reconciliation_invalidates_active_rule_cache_after_definition_mutation(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
) -> None:
    session_id = _register_session(session_manager, project_id, tmp_path)
    manager = RuleDefinitionManager(db)
    AgentDefinitionManager(db).create(
        name="default",
        source="custom",
        definition_json=json.dumps(
            {
                "name": "default",
                "prompts": {"agent": "Run the assigned task."},
                "workflows": {"rule_selectors": {"include": ["tag:default"], "exclude": []}},
            }
        ),
    )
    manager.create(
        name="cached-rule",
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
    db: HubDatabase,
    project_id: str,
) -> None:
    AgentDefinitionManager(db).create(
        name="new-agent",
        source="custom",
        definition_json=json.dumps(
            {
                "name": "new-agent",
                "prompts": {"agent": "Run the assigned task."},
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
    db: HubDatabase,
    project_id: str,
) -> None:
    AgentDefinitionManager(db).create(
        name="new-agent",
        source="custom",
        definition_json=json.dumps(
            {
                "name": "new-agent",
                "prompts": {"agent": "Run the assigned task."},
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


def test_parent_shaped_taskless_session_does_not_restore_step_workflow(
    db: HubDatabase,
    session_manager: SessionManager,
    project_id: str,
    tmp_path: Path,
) -> None:
    _create_worker_agent(db)
    parent_id = _register_session(
        session_manager,
        project_id,
        tmp_path,
        external_id="parent-race",
    )
    session_id = _register_session(
        session_manager,
        project_id,
        tmp_path,
        external_id="child-race",
        parent_session_id=parent_id,
    )
    session = session_manager.get(session_id)
    assert session is not None
    assert session.parent_session_id == parent_id
    assert session.agent_run_id is None
    assert session.agent_depth == 0
    variables = {"_agent_type": "worker"}

    missing = _missing_step_state(db, session_id, variables, session, None)
    created = _ensure_step_instance(db, session_id, variables, session)

    assert missing == []
    assert created is False
    assert AgentStepInstanceManager(db).get_for_session(session_id) is None


def test_parent_shaped_taskless_session_ignores_agent_run_step_fallback(
    db: HubDatabase,
    session_manager: SessionManager,
    project_id: str,
    tmp_path: Path,
) -> None:
    _create_worker_agent(db)
    parent_id = _register_session(
        session_manager,
        project_id,
        tmp_path,
        external_id="parent-fallback",
    )
    session_id = _register_session(
        session_manager,
        project_id,
        tmp_path,
        external_id="child-fallback",
        parent_session_id=parent_id,
    )
    session = session_manager.get(session_id)
    assert session is not None
    agent_run = _AgentRunRecovery(
        id="run-race",
        workflow_name=None,
        agent_name="worker",
        prompt=None,
    )

    missing = _missing_step_state(db, session_id, {}, session, agent_run)

    assert missing == []
    assert AgentStepInstanceManager(db).get_for_session(session_id) is None


@pytest.mark.asyncio
async def test_interactive_persona_reconciliation_keeps_worker_rules_inactive(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
) -> None:
    """Persona prompt identity cannot become lifecycle, selector, or agent-scope identity."""
    agent_manager = AgentDefinitionManager(db)
    agent_manager.create(
        name="default",
        source="custom",
        definition_json=json.dumps(
            {
                "name": "default",
                "prompts": {
                    "persona": "Default prompt.",
                    "agent": "Run the assigned task.",
                },
                "workflows": {"rule_selectors": {"include": ["tag:interactive"], "exclude": []}},
            }
        ),
    )
    agent_manager.create(
        name="qa-reviewer",
        source="custom",
        definition_json=json.dumps(
            {
                "name": "qa-reviewer",
                "prompts": {
                    "persona": "Review prompt.",
                    "agent": "Review the assigned task.",
                },
                "workflows": {"rule_selectors": {"include": ["tag:worker-safety"], "exclude": []}},
            }
        ),
    )
    rule_manager = RuleDefinitionManager(db)
    rule_manager.create(
        name="qa-reviewer-scope-rule",
        source="custom",
        tags=["interactive"],
        definition_json=json.dumps(
            RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_TOOL,
                agent_scope=["qa-reviewer"],
                effects=[
                    RuleEffect(
                        type="set_variable",
                        variable="qa_scope_matched",
                        value=True,
                    )
                ],
            ).model_dump(mode="json"),
        ),
    )
    rule_manager.create(
        name="worker-safety-selector-rule",
        source="custom",
        tags=["worker-safety"],
        definition_json=json.dumps(
            RuleDefinitionBody(
                event=RuleTriggerEvent.BEFORE_TOOL,
                effects=[
                    RuleEffect(
                        type="set_variable",
                        variable="worker_selector_matched",
                        value=True,
                    )
                ],
            ).model_dump(mode="json"),
        ),
    )
    session_id = _register_session(
        session_manager,
        project_id,
        tmp_path,
        external_id="interactive-persona",
    )
    session = session_manager.get(session_id)
    assert session is not None
    assert session.parent_session_id is None
    assert session.agent_run_id is None
    SessionVariableManager(db).merge_variables(
        session_id,
        {
            MARKER_COMPLETED: True,
            MARKER_VERSION: SESSION_ACTIVATION_CONTRACT_VERSION,
            MARKER_HASH: SESSION_ACTIVATION_CONTRACT_HASH,
            "_agent_type": "default",
            "_persona_name": "qa-reviewer",
            "_active_rule_names": ["worker-safety-selector-rule"],
            "_active_skill_names": None,
            "_skill_format": None,
            "_agent_blocked_tools": [],
            "_agent_blocked_mcp_tools": [],
            "is_spawned_agent": False,
            "baseline_dirty_files": [],
            "session_edited_files": [],
        },
    )

    reconcile_session_activation(
        _event(HookEventType.BEFORE_AGENT, session_id, tmp_path),
        handlers,
    )
    variables = _variables(db, session_id)
    rule_event = _event(HookEventType.BEFORE_TOOL, session_id, tmp_path)
    rule_event.data.update({"tool_name": "Bash", "tool_input": {"command": "pwd"}})
    response = await RuleEngine(db).evaluate(rule_event, session_id, variables)

    assert response.decision == "allow"
    assert variables["_agent_type"] == "default"
    assert variables["_persona_name"] == "qa-reviewer"
    assert variables["_active_rule_names"] == ["qa-reviewer-scope-rule"]
    assert "qa_scope_matched" not in variables
    assert "worker_selector_matched" not in variables
    assert AgentStepInstanceManager(db).get_for_session(session_id) is None


def test_spawned_step_agent_restores_workflow_variable_and_instance(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
) -> None:
    _create_worker_agent(db)
    _, child_id = _create_parent_and_child(db, session_manager, project_id, tmp_path)
    SessionVariableManager(db).set_variable(child_id, "assigned_task_id", "#14475")

    result = reconcile_session_activation(
        _event(HookEventType.BEFORE_AGENT, child_id, tmp_path),
        handlers,
    )

    variables = _variables(db, child_id)
    instance = AgentStepInstanceManager(db).get_for_session(child_id)
    assert result.reason == "repaired"
    assert "_step_workflow_name" not in variables
    assert variables["step_workflow_complete"] is False
    assert instance is not None
    assert instance.agent_name == "worker"
    assert instance.current_step == "claim"


def test_completion_seed_after_step_instance_recovery(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
) -> None:
    from gobby.workflows.step_instances import AgentStepInstanceManager

    _create_worker_agent(db)
    _, child_id = _create_parent_and_child(db, session_manager, project_id, tmp_path)
    SessionVariableManager(db).merge_variables(
        child_id,
        {
            "_agent_type": "worker",
            "assigned_task_id": "#14475",
            "is_spawned_agent": True,
        },
    )

    reconcile_session_activation(
        _event(HookEventType.BEFORE_AGENT, child_id, tmp_path),
        handlers,
    )

    variables = _variables(db, child_id)
    instance = AgentStepInstanceManager(db).get_for_session(child_id)
    assert instance is not None
    assert instance.agent_name == "worker"
    assert instance.current_step == "claim"
    assert variables["step_workflow_complete"] is False
    assert "_step_workflow_name" not in variables


@pytest.mark.parametrize("step_name", [None, "worker-steps"])
def test_taskless_spawn_does_not_restore_step_workflow_from_agent_run(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
    step_name: str | None,
) -> None:
    _create_worker_agent(db)
    _, child_id = _create_parent_and_child(db, session_manager, project_id, tmp_path)
    initial_variables: dict[str, Any] = {
        "_agent_type": "worker",
        "_active_rule_names": [],
        "_active_skill_names": [],
        "_skill_format": None,
        "_agent_blocked_tools": ["Bash"],
        "_agent_blocked_mcp_tools": ["gobby-tasks.close_task"],
        "is_spawned_agent": True,
    }
    if step_name is not None:
        initial_variables["_step_workflow_name"] = step_name
    SessionVariableManager(db).merge_variables(child_id, initial_variables)

    result = reconcile_session_activation(
        _event(HookEventType.BEFORE_AGENT, child_id, tmp_path),
        handlers,
    )

    variables = _variables(db, child_id)
    instance = AgentStepInstanceManager(db).get_for_session(child_id)
    assert "step_workflow_instance" not in result.missing
    assert variables["is_spawned_agent"] is True
    assert instance is None


def test_existing_step_workflow_current_step_is_preserved(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
) -> None:
    _create_worker_agent(db)
    _, child_id = _create_parent_and_child(db, session_manager, project_id, tmp_path)
    AgentStepInstanceManager(db).save(
        make_step_instance(
            child_id,
            agent_name="worker",
            current_step="implement",
            variables={"ticket": "14475"},
        )
    )
    SessionVariableManager(db).merge_variables(
        child_id,
        {
            "_agent_type": "worker",
            "is_spawned_agent": True,
        },
    )

    reconcile_session_activation(_event(HookEventType.BEFORE_AGENT, child_id, tmp_path), handlers)

    instance = AgentStepInstanceManager(db).get_for_session(child_id)
    assert instance is not None
    assert instance.current_step == "implement"


def test_stale_spawned_flag_is_repaired_from_session_depth(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
) -> None:
    child_id = _register_session(
        session_manager,
        project_id,
        tmp_path,
        external_id="child-external",
        agent_depth=1,
    )
    SessionVariableManager(db).merge_variables(
        child_id,
        {
            MARKER_COMPLETED: True,
            MARKER_VERSION: SESSION_ACTIVATION_CONTRACT_VERSION,
            MARKER_HASH: SESSION_ACTIVATION_CONTRACT_HASH,
            "_agent_type": "default",
            "_active_rule_names": [],
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
        _event(HookEventType.BEFORE_TOOL, child_id, tmp_path),
        handlers,
    )

    variables = _variables(db, child_id)
    assert result.changed is True
    assert variables["is_spawned_agent"] is True


@pytest.mark.asyncio
async def test_spawned_flag_survives_lagging_terminal_pickup_refresh(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
) -> None:
    RuleDefinitionManager(db).create(
        name="autonomous-only",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            audience="autonomous",
            effects=[RuleEffect(type="block", tools=["Bash"], reason="autonomous only")],
        ).model_dump_json(),
        enabled=True,
        priority=10,
    )
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
    )
    LocalAgentRunManager(db).create(
        parent_session_id=parent_id,
        provider="claude",
        prompt="do the work",
        agent_name="worker",
        child_session_id=child_id,
        run_id="90e65240-4167-55c2-84df-72f933aee3a8",
    )
    SessionVariableManager(db).merge_variables(
        child_id,
        {
            MARKER_COMPLETED: True,
            MARKER_VERSION: SESSION_ACTIVATION_CONTRACT_VERSION,
            MARKER_HASH: SESSION_ACTIVATION_CONTRACT_HASH,
            "_agent_type": "default",
            "_active_rule_names": ["autonomous-only"],
            "_active_skill_names": None,
            "_skill_format": None,
            "_agent_blocked_tools": [],
            "_agent_blocked_mcp_tools": [],
            "is_spawned_agent": True,
            "baseline_dirty_files": [],
            "session_edited_files": [],
        },
    )

    with patch("gobby.hooks.session_activation._backfill_terminal_pickup", return_value=None):
        reconcile_session_activation(
            _event(HookEventType.BEFORE_TOOL, child_id, tmp_path),
            handlers,
        )

    variables = _variables(db, child_id)
    assert variables["is_spawned_agent"] is True

    audience_event = _event(HookEventType.BEFORE_TOOL, child_id, tmp_path)
    audience_event.data["tool_name"] = "Bash"
    audience_result = await RuleEngine(db).evaluate(
        audience_event,
        session_id=child_id,
        variables=variables,
    )
    assert audience_result.decision == "block"


def test_spawned_flag_clears_after_agent_run_lookup_finds_no_run(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
) -> None:
    session_id = _register_session(session_manager, project_id, tmp_path)
    SessionVariableManager(db).merge_variables(
        session_id,
        {
            MARKER_COMPLETED: True,
            MARKER_VERSION: SESSION_ACTIVATION_CONTRACT_VERSION,
            MARKER_HASH: SESSION_ACTIVATION_CONTRACT_HASH,
            "_agent_type": "default",
            "_active_rule_names": [],
            "_active_skill_names": None,
            "_skill_format": None,
            "_agent_blocked_tools": [],
            "_agent_blocked_mcp_tools": [],
            "is_spawned_agent": True,
            "baseline_dirty_files": [],
            "session_edited_files": [],
        },
    )

    reconcile_session_activation(
        _event(HookEventType.BEFORE_TOOL, session_id, tmp_path),
        handlers,
    )

    assert _variables(db, session_id)["is_spawned_agent"] is False


def test_baseline_dirty_initializes_once_and_preserves_session_edits(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
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
    assert variables["active_task_id"] is None
    assert variables["task_edited_files"] == {}


def test_pipeline_session_skips_agent_activation_and_reconciles_baseline(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = session_manager.register_session(
        external_id="pipeline-session",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="pipeline",
        project_id=project_id,
        project_path=str(tmp_path),
    )
    SessionVariableManager(db).merge_variables(
        session_id,
        {"_agent_type": "pipeline", "session_edited_files": ["kept.py"]},
    )
    activate = MagicMock(side_effect=AssertionError("pipeline session activated an agent"))
    monkeypatch.setattr(handlers, "_activate_default_agent", activate)
    monkeypatch.setattr(
        "gobby.workflows.git_utils.get_dirty_files_categorized",
        lambda _path: DirtyFiles(tracked={"dirty.py"}, untracked={"new.py"}),
    )

    reconcile_session_activation(_event(HookEventType.BEFORE_AGENT, session_id, tmp_path), handlers)

    variables = _variables(db, session_id)
    activate.assert_not_called()
    assert variables["_agent_type"] == "pipeline"
    assert variables["baseline_dirty_files"] == ["dirty.py", "new.py"]
    assert variables["session_edited_files"] == ["kept.py"]


def test_baseline_dirty_prefers_valid_repo_path_over_unusable_cwd(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    non_repo = tmp_path / "plain"
    non_repo.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    session_id = _register_session(session_manager, project_id, non_repo)
    captured_paths: list[str | None] = []

    def fake_dirty(path: str | None) -> DirtyFiles:
        captured_paths.append(path)
        return DirtyFiles(tracked={"dirty.py"}, untracked=set())

    monkeypatch.setattr("gobby.workflows.git_utils.get_dirty_files_categorized", fake_dirty)
    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="external-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"cwd": str(non_repo), "project_path": str(repo)},
        metadata={"_platform_session_id": session_id},
    )

    reconcile_session_activation(event, handlers)

    assert captured_paths == [str(repo.resolve())]
    variables = _variables(db, session_id)
    assert variables["baseline_dirty_files"] == ["dirty.py"]


def test_terminal_pickup_metadata_backfills_from_agent_runs(
    db: HubDatabase,
    session_manager: SessionManager,
    handlers: EventHandlers,
    project_id: str,
    tmp_path: Path,
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
    assert session.agent_run_id == "90e65240-4167-55c2-84df-72f933aee3a8"
    assert session.workflow_name == "worker-flow"
    assert session.original_prompt == "do the work"


def test_agent_run_from_row_returns_none_when_required_keys_are_missing() -> None:
    assert _agent_run_from_row({"id": "90e65240-4167-55c2-84df-72f933aee3a8"}) is None


def test_agent_run_from_row_rejects_malformed_required_fields() -> None:
    valid_nullable_fields = {"workflow_name": None, "agent_name": None, "prompt": None}

    assert _agent_run_from_row({"id": "", **valid_nullable_fields}) is None
    assert _agent_run_from_row({"id": 123, **valid_nullable_fields}) is None
    assert (
        _agent_run_from_row(
            {
                "id": "90e65240-4167-55c2-84df-72f933aee3a8",
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
            "id": "90e65240-4167-55c2-84df-72f933aee3a8",
            "workflow_name": "worker-flow",
            "agent_name": "worker",
            "prompt": "do the work",
        }
    )

    assert recovery is not None
    assert recovery.id == "90e65240-4167-55c2-84df-72f933aee3a8"
    assert recovery.workflow_name == "worker-flow"
    assert recovery.agent_name == "worker"
    assert recovery.prompt == "do the work"


@pytest.mark.parametrize(
    ("event_type", "data"),
    [
        (HookEventType.BEFORE_AGENT, {"prompt": "hello"}),
        (
            HookEventType.BEFORE_TOOL,
            {
                "tool_name": "mcp__gobby__call_tool",
                "mcp_server": "gobby-tasks-ops",
                "mcp_tool": "submit_for_review",
            },
        ),
    ],
)
def test_hook_manager_reconciles_before_rules(
    event_type: HookEventType,
    data: dict[str, str],
) -> None:
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
        "event_handlers",
    ):
        setattr(components, name, MagicMock())
    components.webhook_dispatcher.config.enabled = False
    components.health_monitor.get_cached_status.return_value = (True, None, "running", None)
    components.health_monitor.check_now.return_value = True
    components.webhook_dispatcher.get_blocking_decision.return_value = (None, None)

    call_order: list[str] = []

    def record_handler(event: HookEvent) -> HookResponse:
        call_order.append("handler")
        return HookResponse(decision="allow")

    def record_rules(event: HookEvent, blocking_deadline: float | None = None) -> HookResponse:
        call_order.append("rules")
        return HookResponse(decision="allow")

    handler = MagicMock(side_effect=record_handler)
    components.event_handlers.get_handler.return_value = handler
    components.workflow_handler.handle.side_effect = record_rules

    event = HookEvent(
        event_type=event_type,
        session_id="external-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
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
        manager = HookManager()
        with (
            patch.object(manager._session_lookup, "resolve", return_value=None),
            patch.object(manager._enricher, "enrich"),
        ):
            manager._handle_internal(event)

    assert len(call_order) == 3
    assert call_order == ["reconcile", "rules", "handler"]

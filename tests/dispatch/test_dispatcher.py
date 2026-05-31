"""Dispatcher heartbeat scanner tests."""

import asyncio
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from gobby.dispatch.actions import (
    AppendAuditMarkerAction,
    CreateIsolationAction,
    SpawnAgentAction,
    StartPipelineAction,
    StartStageAction,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_affected_files import TaskAffectedFileManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._artifacts import TaskArtifactManager
from gobby.storage.tasks._crud import get_task, update_task
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from tests.storage.tasks._stage_test_helpers import initialize_manifest, set_stage_state, spec

pytestmark = pytest.mark.unit


_LEGACY_STAGE_MAP = {
    "expanding": "expansion",
    "holistic_review": "holistic_qa",
    "in_development": "development",
    "merged": "merge",
}


def _task(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    title: str = "Dispatch task",
    **fields: Any,
) -> Task:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title=title)
    legacy_lifecycle = fields.pop("lifecycle", None)
    legacy_status = fields.pop("status", None)
    stage_name = fields.pop(
        "stage_name",
        _LEGACY_STAGE_MAP.get(str(legacy_lifecycle), "development"),
    )
    stage_state = fields.pop("stage_state", "ready")
    update_task(
        temp_db,
        task.id,
        allow_automation=fields.pop("allow_automation", True),
        task_type=fields.pop("task_type", "task"),
        assigned_agent=fields.pop("assigned_agent", "backend-developer"),
        isolation=fields.pop("isolation", "none"),
        claimed_by_session_id=fields.pop("claimed_by_session_id", None),
        **fields,
    )
    initialize_manifest(temp_db, task.id, [spec(stage_name, 0)])
    set_stage_state(temp_db, task.id, stage_name, stage_state)
    if legacy_status == "closed":
        temp_db.execute(
            "UPDATE tasks SET closed_at = %s, closed_reason = %s WHERE id = %s",
            (datetime.now(UTC).isoformat(), "test_terminal", task.id),
        )
    return get_task(temp_db, task.id)


def _parent_with_stage_order(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    *,
    expansion_state: str,
    development_state: str = "ready",
) -> Task:
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(
        project_id=sample_project["id"],
        title=f"Parent {expansion_state}",
        task_type="epic",
    )
    update_task(
        temp_db,
        parent.id,
        allow_automation=False,
        task_type="epic",
        isolation="none",
    )
    initialize_manifest(
        temp_db,
        parent.id,
        [spec("planning", 0), spec("expansion", 1), spec("development", 2)],
    )
    set_stage_state(temp_db, parent.id, "planning", "done")
    set_stage_state(temp_db, parent.id, "expansion", expansion_state)
    set_stage_state(temp_db, parent.id, "development", development_state)
    return get_task(temp_db, parent.id)


def _mutex_storage(temp_db: HubDatabase) -> TaskDispatchMutexManager:
    storage = TaskDispatchMutexManager(temp_db)
    storage.ensure_table()
    return storage


def _session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_id: str = "session-1",
) -> str:
    temp_db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (session_id, session_id, "machine-1", "test", sample_project["id"]),
    )
    return session_id


def _audit_action(task_id: str) -> AppendAuditMarkerAction:
    return AppendAuditMarkerAction(task_id=task_id, heading="Dispatch", body="marker")


@pytest.mark.asyncio
async def test_append_audit_marker_is_exact_marker_idempotent(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project)

    assert await dispatcher.append_audit_marker(temp_db, task.id, "Dispatch", "marker") is True
    assert await dispatcher.append_audit_marker(temp_db, task.id, "Dispatch", "marker") is False
    assert await dispatcher.append_audit_marker(temp_db, task.id, "Dispatch", "other") is True
    description = get_task(temp_db, task.id).description or ""
    assert description.count("### Dispatch\n\nmarker") == 1
    assert description.count("### Dispatch") == 2


@pytest.mark.asyncio
async def test_append_audit_marker_only_dedupes_trailing_marker(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.dispatch import dispatcher
    from gobby.dispatch.audit import audit_marker_text

    task = _task(temp_db, sample_project)
    marker = audit_marker_text("Dispatch", "marker")
    update_task(temp_db, task.id, description=f"Earlier note{marker}\n\nLater note")

    assert await dispatcher.append_audit_marker(temp_db, task.id, "Dispatch", "marker") is True
    description = get_task(temp_db, task.id).description or ""
    assert description.count(marker) == 2


@pytest.mark.asyncio
async def test_append_audit_marker_returns_false_on_db_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gobby.dispatch import dispatcher

    broken_db = MagicMock()
    broken_db.fetchone.side_effect = psycopg.OperationalError("database unavailable")
    caplog.set_level(logging.WARNING, logger="gobby.dispatch.audit")

    assert await dispatcher.append_audit_marker(broken_db, "task-1", "Dispatch", "marker") is False
    assert "Failed to append dispatch audit marker for task task-1" in caplog.text


def test_development_prompt_includes_persisted_holistic_failure_context(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Development prompt includes persisted holistic failure context."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import rules
    from gobby.dispatch.dispatcher import build_context

    sync_bundled_agents(temp_db)
    task = _task(
        temp_db,
        sample_project,
        title="Reopened leaf",
        stage_state="in_progress",
        assigned_agent="backend-developer",
    )
    temp_db.execute(
        """
        INSERT INTO task_comments (
            id, task_id, parent_comment_id, author, author_type, body, created_at, updated_at
        )
        VALUES (
            'comment-holistic-followup', %s, NULL, 'holistic-reviewer', 'system',
            '## Holistic QA Follow-Up\n\nFix the dialect parity suite.', CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        )
        """,
        (task.id,),
    )

    action = rules.development_rule(task, build_context(temp_db, task))

    assert isinstance(action, SpawnAgentAction)
    assert "Previous failure context for this follow-up work" in action.prompt
    assert "Fix the dialect parity suite." in action.prompt


class _FakePipeline:
    name = "expand-task"
    enabled = True
    deprecated = False
    steps = []

    def model_dump_json(self) -> str:
        return '{"name":"expand-task"}'


class _FakePipelineLoader:
    async def load_pipeline(self, name: str):
        return _FakePipeline() if name == "expand-task" else None


class _FakePipelineExecutor:
    def __init__(self) -> None:
        self.loader = _FakePipelineLoader()
        self.calls: list[dict[str, object]] = []
        self.called = asyncio.Event()

    async def execute(self, **kwargs):
        self.record_call(kwargs)
        return SimpleNamespace(id=kwargs["execution_id"], status="completed")

    def record_call(self, call: dict[str, object]) -> None:
        self.calls.append(call)
        self.called.set()


async def _wait_for_executor_calls(
    executor: _FakePipelineExecutor,
) -> list[dict[str, object]]:
    await asyncio.wait_for(executor.called.wait(), timeout=1)
    return executor.calls


def _pipeline_action(task_id: str) -> StartPipelineAction:
    return StartPipelineAction(
        task_id=task_id,
        task_ref="#1",
        stage_name="expansion",
        pipeline_name="expand-task",
        dispatch_inputs={"task_id": "${{ task_id }}"},
    )


def test_candidate_filter_excludes_claimed_leased_blocked_terminal(temp_db, sample_project) -> None:
    """Candidate filter excludes claimed leased blocked terminal."""
    from gobby.storage.tasks import _crud

    ready = _task(temp_db, sample_project, "ready")
    _task(
        temp_db, sample_project, "claimed", claimed_by_session_id=_session(temp_db, sample_project)
    )
    leased = _task(temp_db, sample_project, "leased")
    terminal = _task(temp_db, sample_project, "terminal", lifecycle="merged", status="closed")
    blocked = _task(temp_db, sample_project, "blocked")
    blocker = _task(temp_db, sample_project, "blocker", allow_automation=False)

    _mutex_storage(temp_db).acquire_mutex(
        leased.id,
        holder="other",
        kind="test",
        ttl_seconds=30,
    )
    temp_db.execute(
        "UPDATE tasks SET closed_at = %s WHERE id = %s",
        (datetime.now(UTC).isoformat(), terminal.id),
    )
    temp_db.execute(
        """
        INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at)
        VALUES (%s, %s, 'blocks', %s)
        """,
        (blocked.id, blocker.id, datetime.now(UTC).isoformat()),
    )

    candidates = _crud.list_automation_candidates(temp_db, project_id=sample_project["id"])

    assert [candidate.id for candidate in candidates] == [ready.id]
    assert not _crud.is_blocked_by_deps(candidates[0])


@pytest.mark.asyncio
async def test_heartbeat_blocks_child_development_while_parent_expansion_needs_review(
    temp_db,
    sample_project,
) -> None:
    from gobby.dispatch import dispatcher
    from gobby.storage.tasks import _crud

    parent = _parent_with_stage_order(
        temp_db,
        sample_project,
        expansion_state="needs_review",
    )
    child = _task(
        temp_db,
        sample_project,
        title="Blocked child",
        parent_task_id=parent.id,
        stage_name="development",
        stage_state="ready",
    )

    candidates = _crud.list_automation_candidates(temp_db, project_id=sample_project["id"])
    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert child.id not in {candidate.id for candidate in candidates}
    assert result.executed == 0
    assert LocalTaskManager(temp_db).stage_states.get(child.id, "development").state == "ready"


@pytest.mark.parametrize("parent_development_state", ["ready", "in_progress"])
@pytest.mark.asyncio
async def test_heartbeat_allows_child_development_after_parent_expansion_done(
    parent_development_state: str,
    temp_db,
    sample_project,
) -> None:
    from gobby.dispatch import dispatcher

    parent = _parent_with_stage_order(
        temp_db,
        sample_project,
        expansion_state="done",
        development_state=parent_development_state,
    )
    child = _task(
        temp_db,
        sample_project,
        title=f"Allowed child {parent_development_state}",
        parent_task_id=parent.id,
        stage_name="development",
        stage_state="ready",
    )

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 1
    assert LocalTaskManager(temp_db).stage_states.get(child.id, "development").state == (
        "in_progress"
    )


@pytest.mark.asyncio
async def test_heartbeat_records_gated_holistic_root_before_reopened_descendant(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.dispatch import dispatcher

    manager = LocalTaskManager(temp_db)
    root = manager.create_task(
        project_id=sample_project["id"],
        title="Holistic root",
        task_type="epic",
    )
    update_task(temp_db, root.id, allow_automation=True, isolation="none", task_type="epic")
    initialize_manifest(
        temp_db,
        root.id,
        [spec("development", 0), spec("holistic_qa", 1), spec("merge", 2)],
    )
    set_stage_state(temp_db, root.id, "development", "done")
    set_stage_state(temp_db, root.id, "holistic_qa", "ready")
    phase = manager.create_task(
        project_id=sample_project["id"],
        title="Integrated phase",
        parent_task_id=root.id,
    )
    child = _task(
        temp_db,
        sample_project,
        title="Reopened child",
        parent_task_id=phase.id,
        stage_name="development",
        stage_state="ready",
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        max_actions=1,
    )

    assert result.executed == 1
    assert LocalTaskManager(temp_db).stage_states.get(child.id, "development").state == "ready"
    assert LocalTaskManager(temp_db).stage_states.get(root.id, "holistic_qa").state == "ready"
    root_description = get_task(temp_db, root.id).description or ""
    assert "### Holistic QA deferred" in root_description


def test_count_active_agents_scopes_by_parent_session_project(temp_db, sample_project) -> None:
    """Count active agents scopes by parent session project."""
    from gobby.dispatch.dispatcher import count_active_agents
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager

    sessions = SessionManager(temp_db)
    agents = LocalAgentRunManager(temp_db)
    other_project = LocalProjectManager(temp_db).create(name="other-project")
    parent_a = sessions.register(
        external_id="parent-a",
        machine_id="machine-1",
        source="test",
        project_id=sample_project["id"],
    )
    parent_b = sessions.register(
        external_id="parent-b",
        machine_id="machine-1",
        source="test",
        project_id=other_project.id,
    )
    run_a = agents.create(parent_session_id=parent_a.id, provider="codex", prompt="a")
    run_b = agents.create(parent_session_id=parent_b.id, provider="codex", prompt="b")
    run_done = agents.create(parent_session_id=parent_a.id, provider="codex", prompt="done")
    agents.start(run_a.id)
    agents.start(run_b.id)
    agents.complete(run_done.id, result="done")

    assert count_active_agents(temp_db) == 2
    assert count_active_agents(temp_db, project_id=sample_project["id"]) == 1
    assert count_active_agents(temp_db, project_id=other_project.id) == 1


@pytest.mark.asyncio
async def test_max_active_agents_cap(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    """Max active agents cap."""
    from gobby.dispatch import dispatcher

    _task(temp_db, sample_project)
    spawned: list[object] = []
    monkeypatch.setattr(dispatcher, "count_active_agents", lambda *args, **kwargs: 2)
    monkeypatch.setattr(dispatcher, "MAX_ACTIVE_AGENTS", 2)
    monkeypatch.setattr(dispatcher, "spawn_agent", lambda *args, **kwargs: spawned.append(args))

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.cap_reached is True
    assert spawned == []


@pytest.mark.asyncio
async def test_run_heartbeat_serializes_overlapping_development_start_actions(
    temp_db,
    sample_project,
) -> None:
    """Run heartbeat serializes overlapping development start actions."""
    from gobby.dispatch import dispatcher

    first = _task(temp_db, sample_project, "shared refactor", priority=1)
    second = _task(temp_db, sample_project, "shared follow-up", priority=2)
    af_manager = TaskAffectedFileManager(temp_db)
    af_manager.set_files(first.id, ["src/gobby/config/bootstrap.py"], source="expansion")
    af_manager.set_files(second.id, ["src/gobby/config/bootstrap.py"], source="expansion")

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    stage_states = LocalTaskManager(temp_db).stage_states
    assert result.executed == 1
    assert result.skipped == 1
    assert stage_states.get(first.id, "development").state == "in_progress"
    assert stage_states.get(second.id, "development").state == "ready"


@pytest.mark.asyncio
async def test_run_heartbeat_allows_disjoint_development_write_sets(
    temp_db,
    sample_project,
) -> None:
    """Run heartbeat allows disjoint development write sets."""
    from gobby.dispatch import dispatcher

    first = _task(temp_db, sample_project, "config refactor", priority=1)
    second = _task(temp_db, sample_project, "routes follow-up", priority=2)
    af_manager = TaskAffectedFileManager(temp_db)
    af_manager.set_files(first.id, ["src/gobby/config/bootstrap.py"], source="expansion")
    af_manager.set_files(second.id, ["src/gobby/servers/routes/build.py"], source="expansion")

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    stage_states = LocalTaskManager(temp_db).stage_states
    assert result.executed == 2
    assert result.skipped == 0
    assert stage_states.get(first.id, "development").state == "in_progress"
    assert stage_states.get(second.id, "development").state == "in_progress"


@pytest.mark.asyncio
async def test_run_heartbeat_max_actions_stops_after_one_lifecycle_action(
    temp_db,
    sample_project,
) -> None:
    """A quick dispatcher pass runs exactly one action even with multiple ready tasks."""
    from gobby.dispatch import dispatcher

    first = _task(temp_db, sample_project, "first quick action")
    second = _task(temp_db, sample_project, "second quick action")

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        max_actions=1,
    )

    stage_states = LocalTaskManager(temp_db).stage_states
    assert result.executed == 1
    assert result.cap_reached is True
    assert stage_states.get(first.id, "development").state == "in_progress"
    assert stage_states.get(second.id, "development").state == "ready"


@pytest.mark.asyncio
async def test_run_heartbeat_blocks_ready_task_behind_active_overlapping_write_set(
    temp_db,
    sample_project,
) -> None:
    """Run heartbeat blocks ready task behind active overlapping write set."""
    from gobby.dispatch import dispatcher

    owner_session_id = _session(temp_db, sample_project, "owner-session")
    active = _task(
        temp_db,
        sample_project,
        "active config work",
        stage_state="in_progress",
        claimed_by_session_id=owner_session_id,
    )
    waiting = _task(temp_db, sample_project, "waiting config work")
    af_manager = TaskAffectedFileManager(temp_db)
    af_manager.set_files(active.id, ["src/gobby/config/bootstrap.py"], source="expansion")
    af_manager.set_files(waiting.id, ["src/gobby/config/bootstrap.py"], source="expansion")

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 0
    assert result.skipped == 1
    assert LocalTaskManager(temp_db).stage_states.get(waiting.id, "development").state == "ready"


@pytest.mark.asyncio
async def test_run_heartbeat_skips_spawn_when_daemon_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Run heartbeat skips spawn when daemon not ready."""
    from gobby.dispatch import dispatcher

    _task(temp_db, sample_project)
    spawned: list[object] = []
    services = SimpleNamespace(startup_ready=False, shutdown_in_progress=False)
    monkeypatch.setattr(dispatcher, "spawn_agent", lambda *args, **kwargs: spawned.append(args))

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    assert result.reason == "daemon_startup_not_ready"
    assert result.executed == 0
    assert spawned == []


@pytest.mark.asyncio
async def test_cancelled_spawn_releases_no_run_mutex(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Cancelled spawn releases no run mutex."""
    from gobby.dispatch import dispatcher
    from gobby.dispatch.mutex import RuntimeDispatchMutex

    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    mutex = RuntimeDispatchMutex(
        storage,
        task_id=task.id,
        holder="dispatcher",
        action_kind="heartbeat",
        ttl_seconds=600,
    )
    mutex.__enter__()

    async def cancelled_spawn(*_args: object, **_kwargs: object) -> str:
        raise asyncio.CancelledError

    monkeypatch.setattr(dispatcher, "spawn_agent", cancelled_spawn)

    with pytest.raises(asyncio.CancelledError):
        await dispatcher.execute_action(
            SpawnAgentAction(
                task_id=task.id,
                task_ref=f"#{task.seq_num}",
                agent_slug="backend-developer",
                prompt="do work",
            ),
            mutex=mutex,
            db=temp_db,
        )

    assert storage.get_mutex(task.id) is None


@pytest.mark.asyncio
async def test_mutex_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Mutex lifecycle."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project)
    storage = _mutex_storage(temp_db)
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: _audit_action(task.id),
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert storage.get_mutex(task.id) is None
    assert "### Dispatch" in get_task(temp_db, task.id).description


@pytest.mark.asyncio
async def test_toctou_skip_on_changed_tuple(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    """Toctou skip on changed tuple."""
    from gobby.dispatch import dispatcher

    _task(temp_db, sample_project)
    executed: list[object] = []

    def reload_changed(task_id: str, **kwargs):
        return get_task(temp_db, task_id) if executed else _task_changed(temp_db, task_id)

    monkeypatch.setattr(dispatcher, "reload_candidate", reload_changed)
    monkeypatch.setattr(
        dispatcher, "execute_action", lambda action, **kwargs: executed.append(action)
    )

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.skipped == 1
    assert executed == []


def _task_changed(temp_db, task_id: str):
    set_stage_state(temp_db, task_id, "development", "in_progress")
    return get_task(temp_db, task_id)


@pytest.mark.asyncio
async def test_first_match_action_executed(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    """First match action executed."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project)
    executed: list[object] = []
    action = _audit_action(task.id)
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    monkeypatch.setattr(
        dispatcher, "execute_action", lambda action, **kwargs: executed.append(action)
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert executed == [action]


@pytest.mark.asyncio
async def test_spawn_action_links_run_id(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    """Spawn action links run id."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project)
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref="#1",
        agent_slug="backend-developer",
        prompt="go",
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    monkeypatch.setattr(dispatcher, "spawn_agent", lambda *args, **kwargs: "run-1")

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert storage.get_mutex(task.id).run_id == "run-1"


@pytest.mark.asyncio
async def test_spawn_action_uses_services_and_records_agent_run(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    """Spawn action uses services and records agent run."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )
    spawn_kwargs: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs):
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=kwargs["parent_session_id"],
            provider="codex",
            prompt=kwargs["prompt"],
            agent_name=kwargs["agent_lookup_name"],
            task_id=task.id,
            run_id="run-services",
        )
        return {"success": True, "run_id": run.id, "isolation": "none"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    run = LocalAgentRunManager(temp_db).get("run-services")
    launcher = session_manager.get(run.parent_session_id)
    assert result.executed == 1
    assert run.agent_name == "backend-developer"
    assert run.task_id == task.id
    assert spawn_kwargs["task_id"] == task.id
    assert spawn_kwargs["initial_variables"]["_step_workflow_name"] == "backend-developer-steps"
    assert launcher.source == "dispatcher_launcher"
    assert storage.get_mutex(task.id).run_id == "run-services"


@pytest.mark.parametrize("agent_slug", ["planner", "plan-adversary"])
@pytest.mark.asyncio
async def test_planning_agents_use_main_context_despite_worktree_task(
    agent_slug: str,
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Planning agents use main context despite worktree task."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_name="planning",
        stage_state="in_progress",
        isolation="worktree",
        assigned_agent=agent_slug,
    )
    stale_worktree_id = f"wt-{agent_slug}"
    stale_worktree_path = f"/tmp/missing-{agent_slug}-worktree"
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path=stale_worktree_path,
        worktree_id=stale_worktree_id,
        base_commit_sha="old-base",
        target_branch="main",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug=agent_slug,
        prompt="plan",
        initial_variables={"stage_name": "planning", "stage_state": "in_progress"},
    )
    spawn_kwargs: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=task.id,
            run_id=f"run-{agent_slug}",
        )
        return {"success": True, "run_id": run.id, "isolation": kwargs["isolation"]}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )
    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)

    assert result.executed == 1
    assert spawn_kwargs["isolation"] == "none"
    assert spawn_kwargs["worktree_id"] is None
    assert spawn_kwargs["clone_id"] is None
    assert artifacts.worktree_id == stale_worktree_id
    assert artifacts.worktree_path == stale_worktree_path
    assert artifacts.base_commit_sha == "old-base"


@pytest.mark.asyncio
async def test_expansion_review_uses_main_context_despite_worktree_task(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Expansion review uses main context despite worktree task."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_name="expansion",
        stage_state="needs_review",
        isolation="worktree",
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path="/tmp/missing-expansion-worktree",
        worktree_id="wt-expansion",
        base_commit_sha="old-base",
        target_branch="main",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="expansion-qa",
        prompt="review expansion",
        initial_variables={"stage_name": "expansion", "stage_state": "needs_review"},
    )
    spawn_kwargs: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=task.id,
            run_id="run-expansion-main",
        )
        return {"success": True, "run_id": run.id, "isolation": kwargs["isolation"]}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )
    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)

    assert result.executed == 1
    assert spawn_kwargs["isolation"] == "none"
    assert spawn_kwargs["worktree_id"] is None
    assert spawn_kwargs["clone_id"] is None
    assert artifacts.worktree_id == "wt-expansion"
    assert artifacts.worktree_path == "/tmp/missing-expansion-worktree"


@pytest.mark.asyncio
async def test_backend_developer_inherits_task_worktree_isolation(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Backend developer inherits task worktree isolation."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_state="in_progress",
        isolation="worktree",
        assigned_agent="backend-developer",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )
    spawn_kwargs: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=task.id,
            run_id="run-backend-inherit",
        )
        return {"success": True, "run_id": run.id, "isolation": kwargs["isolation"]}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    assert result.executed == 1
    assert spawn_kwargs["isolation"] == "worktree"


@pytest.mark.asyncio
async def test_spawn_action_subscribes_build_coordinator_completion(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    """Spawn action subscribes build coordinator completion."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    coordinator = session_manager.register(
        external_id="coord-ext",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
        title="Coordinator",
    )
    task = _task(temp_db, sample_project, stage_state="in_progress")
    BuildHistoryStorage(temp_db).record_run(
        project_id=sample_project["id"],
        root_task_id=task.id,
        input_ref=f"#{task.seq_num}",
        action="build",
        summary={"coordinator_session_id": coordinator.id},
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=task.id,
            run_id="run-coordinated",
        )
        return {"success": True, "run_id": run.id, "isolation": "none"}

    completion_registry = MagicMock()
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
        completion_registry=completion_registry,
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    assert result.executed == 1
    completion_registry.register.assert_called_once_with(
        "run-coordinated",
        subscribers=[coordinator.id],
    )
    subscribers = LocalPipelineExecutionManager(temp_db, project_id="").get_completion_subscribers(
        "run-coordinated"
    )
    assert subscribers == [coordinator.id]


def test_spawn_action_skips_cross_project_build_coordinator_completion(
    temp_db,
    sample_project,
) -> None:
    """Spawn action skips cross project build coordinator completion."""
    from gobby.dispatch.spawn import _subscribe_build_coordinator_completion
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager

    other_project = LocalProjectManager(temp_db).create(name="other-project")
    session_manager = SessionManager(temp_db)
    coordinator = session_manager.register(
        external_id="coord-ext",
        machine_id="machine-1",
        source="codex",
        project_id=other_project.id,
        title="Coordinator",
    )
    task = _task(temp_db, sample_project, stage_state="in_progress")
    BuildHistoryStorage(temp_db).record_run(
        project_id=sample_project["id"],
        root_task_id=task.id,
        input_ref=f"#{task.seq_num}",
        action="build",
        summary={"coordinator_session_id": coordinator.id},
    )
    completion_registry = MagicMock()
    services = SimpleNamespace(
        session_manager=session_manager,
        completion_registry=completion_registry,
    )

    _subscribe_build_coordinator_completion(
        db=temp_db,
        project_id=sample_project["id"],
        task_id=task.id,
        run_id="run-cross-project",
        services=services,
    )

    completion_registry.register.assert_not_called()
    subscribers = LocalPipelineExecutionManager(temp_db, project_id="").get_completion_subscribers(
        "run-cross-project"
    )
    assert subscribers == []


def test_spawn_action_allows_explicit_cross_project_build_coordinator_completion(
    temp_db,
    sample_project,
) -> None:
    """Spawn action subscribes a cross-project coordinator when build metadata authorizes it."""
    from gobby.dispatch.spawn import _subscribe_build_coordinator_completion
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager

    other_project = LocalProjectManager(temp_db).create(name="other-explicit-project")
    session_manager = SessionManager(temp_db)
    coordinator = session_manager.register(
        external_id="coord-ext-explicit",
        machine_id="machine-1",
        source="codex",
        project_id=other_project.id,
        title="Coordinator",
    )
    task = _task(temp_db, sample_project, stage_state="in_progress")
    BuildHistoryStorage(temp_db).record_run(
        project_id=sample_project["id"],
        root_task_id=task.id,
        input_ref=f"#{task.seq_num}",
        action="build",
        summary={
            "build_project_id": sample_project["id"],
            "coordinator_project_id": other_project.id,
            "coordinator_session_id": coordinator.id,
        },
    )
    completion_registry = MagicMock()
    services = SimpleNamespace(
        session_manager=session_manager,
        completion_registry=completion_registry,
    )

    _subscribe_build_coordinator_completion(
        db=temp_db,
        project_id=sample_project["id"],
        task_id=task.id,
        run_id="run-cross-project-explicit",
        services=services,
    )

    completion_registry.register.assert_called_once_with(
        "run-cross-project-explicit",
        subscribers=[coordinator.id],
    )
    subscribers = LocalPipelineExecutionManager(temp_db, project_id="").get_completion_subscribers(
        "run-cross-project-explicit"
    )
    assert subscribers == [coordinator.id]


@pytest.mark.asyncio
async def test_spawn_action_without_coordinator_does_not_subscribe_launcher(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    """Spawn action without coordinator does not subscribe launcher."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.pipelines import LocalPipelineExecutionManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=task.id,
            run_id="run-unattended",
        )
        return {"success": True, "run_id": run.id, "isolation": "none"}

    completion_registry = MagicMock()
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
        completion_registry=completion_registry,
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    assert result.executed == 1
    completion_registry.register.assert_not_called()
    subscribers = LocalPipelineExecutionManager(temp_db, project_id="").get_completion_subscribers(
        "run-unattended"
    )
    assert subscribers == []


@pytest.mark.asyncio
async def test_spawn_action_clears_missing_worktree_artifact_before_reuse(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    """Spawn action clears missing worktree artifact before reuse."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_state="in_progress",
        isolation="worktree",
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path="/tmp/missing-worktree",
        worktree_id="wt-missing",
        base_commit_sha="old-base",
        target_branch="main",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )
    spawn_kwargs: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs):
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=kwargs["parent_session_id"],
            provider="codex",
            prompt=kwargs["prompt"],
            agent_name=kwargs["agent_lookup_name"],
            task_id=task.id,
            run_id="run-fresh-worktree",
        )
        return {"success": True, "run_id": run.id, "isolation": "worktree"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )
    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)

    assert result.executed == 1
    assert spawn_kwargs["worktree_id"] is None
    assert artifacts.worktree_id is None
    assert artifacts.worktree_path is None
    assert artifacts.base_commit_sha is None
    assert artifacts.target_branch == "main"


@pytest.mark.asyncio
async def test_leaf_spawn_recovers_parent_integration_target_branch(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    """Leaf spawn recovers parent integration target branch."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    parent = _task(
        temp_db,
        sample_project,
        title="Phase epic",
        task_type="epic",
        allow_automation=False,
    )
    leaf = _task(
        temp_db,
        sample_project,
        title="Leaf implementation",
        parent_task_id=parent.id,
        stage_state="in_progress",
        isolation="worktree",
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        parent.id,
        target_branch="main",
        integration_branch="gobby/integration/phase",
    )
    action = SpawnAgentAction(
        task_id=leaf.id,
        task_ref=f"#{leaf.seq_num}",
        agent_slug="backend-developer",
        prompt="go",
    )
    spawn_kwargs: dict[str, object] = {}

    def unexpected_prepare(**_kwargs: object) -> None:
        raise AssertionError("leaf spawn should not use holistic epic workspace preparation")

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=leaf.id,
            run_id="run-leaf-integration-base",
        )
        return {"success": True, "run_id": run.id, "isolation": "worktree"}

    monkeypatch.setattr(
        "gobby.dispatch.spawn.ensure_epic_integration_workspaces",
        unexpected_prepare,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )
    artifacts = TaskArtifactManager(temp_db).get_artifacts(leaf.id)

    assert result.executed == 1
    assert spawn_kwargs["base_branch"] == "gobby/integration/phase"
    assert spawn_kwargs["worktree_id"] is None
    assert artifacts.target_branch == "gobby/integration/phase"


@pytest.mark.asyncio
async def test_epic_holistic_spawn_refreshes_and_reuses_integration_workspace(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    """Epic holistic spawn refreshes and reuses integration workspace."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_name="holistic_qa",
        stage_state="in_progress",
        task_type="epic",
        isolation="worktree",
        assigned_agent="holistic-reviewer",
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path="/tmp/stale-parent",
        worktree_id="wt-stale",
        base_commit_sha="old-base",
        target_branch="main",
        integration_branch="gobby/integration/parent",
        integration_workspace_id="wt-integration",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="holistic-reviewer",
        prompt="review",
        initial_variables={"stage_name": "holistic_qa", "stage_state": "in_progress"},
    )
    prepare_calls: list[dict[str, object]] = []
    spawn_kwargs: dict[str, object] = {}

    def fake_prepare(**kwargs: object) -> None:
        prepare_calls.append(kwargs)

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=task.id,
            run_id="run-holistic",
        )
        return {
            "success": True,
            "run_id": run.id,
            "isolation": "worktree",
            "worktree_id": kwargs["worktree_id"],
            "worktree_path": "/tmp/integration-parent",
        }

    monkeypatch.setattr(
        "gobby.dispatch.spawn.ensure_epic_integration_workspaces",
        fake_prepare,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )
    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)

    assert result.executed == 1
    assert prepare_calls
    assert prepare_calls[0]["root_task"].id == task.id
    assert spawn_kwargs["worktree_id"] == "wt-integration"
    assert spawn_kwargs["clone_id"] is None
    assert artifacts.worktree_id is None
    assert artifacts.worktree_path is None
    assert artifacts.base_commit_sha is None


@pytest.mark.asyncio
async def test_epic_holistic_spawn_promotes_existing_worktree_when_target_missing(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project, tmp_path
) -> None:
    """Epic holistic spawn promotes existing worktree when target missing."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.worktrees import LocalWorktreeManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_name="holistic_qa",
        stage_state="in_progress",
        task_type="epic",
        isolation="worktree",
        assigned_agent="holistic-reviewer",
    )
    worktree_path = tmp_path / "phase-worktree"
    worktree_path.mkdir()
    worktree = LocalWorktreeManager(temp_db).create(
        project_id=sample_project["id"],
        branch_name="task-phase",
        worktree_path=str(worktree_path),
        base_branch="main",
        task_id=task.id,
    )
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path=str(worktree_path),
        worktree_id=worktree.id,
        base_commit_sha="old-base",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="holistic-reviewer",
        prompt="review",
        initial_variables={"stage_name": "holistic_qa", "stage_state": "in_progress"},
    )
    prepare_calls: list[dict[str, object]] = []
    spawn_kwargs: dict[str, object] = {}

    def fake_prepare(**kwargs: object) -> None:
        prepare_calls.append(kwargs)

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        spawn_kwargs.update(kwargs)
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=task.id,
            run_id="run-holistic-recovered",
        )
        return {
            "success": True,
            "run_id": run.id,
            "isolation": "worktree",
            "worktree_id": kwargs["worktree_id"],
            "worktree_path": str(worktree_path),
        }

    monkeypatch.setattr(
        "gobby.dispatch.spawn.ensure_epic_integration_workspaces",
        fake_prepare,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )
    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    stored_worktree = LocalWorktreeManager(temp_db).get(worktree.id)

    assert result.executed == 1
    assert prepare_calls
    assert prepare_calls[0]["target_branch"] == "main"
    assert spawn_kwargs["worktree_id"] == worktree.id
    assert artifacts.target_branch == "main"
    assert artifacts.integration_branch == "task-phase"
    assert artifacts.integration_workspace_id == worktree.id
    assert artifacts.worktree_id is None
    assert stored_worktree is not None
    assert stored_worktree.workspace_role == "integration"


@pytest.mark.asyncio
async def test_epic_holistic_spawn_recovers_missing_target_from_current_branch(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project, tmp_path
) -> None:
    """Epic holistic spawn recovers missing target from current branch."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    LocalProjectManager(temp_db).update(sample_project["id"], repo_path=str(repo))
    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_name="holistic_qa",
        stage_state="in_progress",
        task_type="epic",
        isolation="worktree",
        assigned_agent="holistic-reviewer",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="holistic-reviewer",
        prompt="review",
        initial_variables={"stage_name": "holistic_qa", "stage_state": "in_progress"},
    )
    prepare_calls: list[dict[str, object]] = []

    def fake_prepare(**kwargs: object) -> None:
        prepare_calls.append(kwargs)

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        run = LocalAgentRunManager(temp_db).create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=task.id,
            run_id="run-holistic-current-branch",
        )
        return {"success": True, "run_id": run.id, "isolation": "worktree"}

    monkeypatch.setattr(
        "gobby.dispatch.spawn.ensure_epic_integration_workspaces",
        fake_prepare,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )
    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)

    assert result.executed == 1
    assert prepare_calls[0]["target_branch"] == "main"
    assert artifacts.target_branch == "main"


@pytest.mark.asyncio
async def test_epic_holistic_workspace_conflict_rolls_back_without_heartbeat_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    temp_db,
    sample_project,
) -> None:
    """Epic holistic workspace conflict rolls back without heartbeat error."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.build.workspaces import BuildWorkspaceError
    from gobby.dispatch import dispatcher
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(
        temp_db,
        sample_project,
        stage_name="holistic_qa",
        stage_state="in_progress",
        task_type="epic",
        isolation="worktree",
        assigned_agent="holistic-reviewer",
        dispatch_failure_count=2,
    )
    child = _task(
        temp_db,
        sample_project,
        title="Conflicting child",
        parent_task_id=task.id,
        stage_name="development",
        stage_state="done",
        task_type="feature",
        isolation="worktree",
        assigned_agent="backend-developer",
        status="closed",
    )
    set_stage_state(temp_db, task.id, "holistic_qa", "in_progress", work_attempt_count=3)
    set_stage_state(temp_db, child.id, "development", "done", work_attempt_count=3)
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        target_branch="main",
        integration_branch="gobby/integration/phase",
        integration_workspace_id="wt-integration",
    )
    action = SpawnAgentAction(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        agent_slug="holistic-reviewer",
        prompt="review",
        initial_variables={"stage_name": "holistic_qa", "stage_state": "in_progress"},
    )

    def fail_prepare(**_kwargs: object) -> None:
        raise BuildWorkspaceError("failed to refresh integration workspace: CONFLICT")

    async def unexpected_spawn_agent_impl(**_kwargs: object) -> dict[str, object]:
        raise AssertionError("spawn should not run after workspace preparation failure")

    monkeypatch.setattr(
        "gobby.dispatch.spawn.ensure_epic_integration_workspaces",
        fail_prepare,
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        unexpected_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
    )
    storage = _mutex_storage(temp_db)
    caplog.set_level(logging.ERROR, logger="gobby.dispatch.dispatcher")

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    updated = get_task(temp_db, task.id)
    reopened_child = get_task(temp_db, child.id)
    assert result.executed == 1
    assert storage.get_mutex(task.id) is None
    parent_stage = task_manager.stage_states.get(task.id, "holistic_qa")
    child_stage = task_manager.stage_states.get(child.id, "development")
    assert parent_stage is not None
    assert child_stage is not None
    assert parent_stage.state == "ready"
    assert parent_stage.work_attempt_count == 2
    assert child_stage.state == "ready"
    assert child_stage.work_attempt_count == 0
    assert reopened_child.closed_at is None
    assert updated.dispatch_failure_count == 0
    assert updated.is_escalated is False
    assert "### Dispatch spawn failed" in updated.description
    assert "failed to refresh integration workspace: CONFLICT" in updated.description
    assert "Dispatcher heartbeat candidate failed" not in caplog.text


@pytest.mark.asyncio
async def test_spawn_failure_rolls_stage_ready_and_releases(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    """Spawn failure rolls stage ready and releases."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")

    async def fake_spawn_agent_impl(**_kwargs):
        return {"success": False, "error": "tmux unavailable"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    updated = get_task(temp_db, task.id)
    assert result.executed == 1
    assert storage.get_mutex(task.id) is None
    assert task_manager.stage_states.get(task.id, "development").state == "ready"
    assert updated.dispatch_failure_count == 1
    assert "### Dispatch spawn failed" in updated.description
    assert LocalAgentRunManager(temp_db).get("run-services") is None


@pytest.mark.asyncio
async def test_spawn_unavailable_does_not_mark_task_failed(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Spawn unavailable does not mark task failed."""
    from gobby.dispatch import dispatcher

    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")

    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    updated = get_task(temp_db, task.id)
    assert result.executed == 0
    assert result.skipped == 1
    assert result.reason == "services_missing:database,task_manager,session_manager,agent_runner"
    assert storage.get_mutex(task.id) is None
    assert task_manager.stage_states.get(task.id, "development").state == "in_progress"
    assert updated.dispatch_failure_count == 0
    assert "### Dispatch spawn failed" not in (updated.description or "")


@pytest.mark.asyncio
async def test_unregistered_spawn_records_dispatch_failure_telemetry(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    """Unregistered spawn records dispatch failure telemetry."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")

    async def fake_spawn_agent_impl(**_kwargs):
        return {"success": False, "error": "agent_did_not_register"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    updated = get_task(temp_db, task.id)
    assert result.executed == 1
    assert storage.get_mutex(task.id) is None
    assert task_manager.stage_states.get(task.id, "development").state == "ready"
    assert updated.claimed_by_session_id is None
    assert updated.dispatch_failure_count == 1
    assert "### Dispatch spawn failed" in updated.description
    assert "agent_did_not_register" in updated.description


@pytest.mark.asyncio
async def test_spawn_failure_cleanup_tolerates_already_ready_stage(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    temp_db,
    sample_project,
) -> None:
    """Spawn failure cleanup tolerates already ready stage."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks._stage_types import IllegalStageTransitionError

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")

    async def fake_spawn_agent_impl(**_kwargs):
        return {"success": False, "error": "code_index_preflight_failed"}

    def racing_fail_stage(*_args, **_kwargs):
        set_stage_state(temp_db, task.id, "development", "ready")
        raise IllegalStageTransitionError("development", "ready", "fail_stage", "required")

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(task_manager.stage_states, "fail_stage", racing_fail_stage)
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
    )

    with caplog.at_level(logging.WARNING):
        result = await dispatcher.run_heartbeat(
            db=temp_db,
            project_id=sample_project["id"],
            services=services,
        )

    updated = get_task(temp_db, task.id)
    assert result.executed == 1
    assert storage.get_mutex(task.id) is None
    assert task_manager.stage_states.get(task.id, "development").state == "ready"
    assert updated.dispatch_failure_count == 1
    assert "### Dispatch spawn failed" in updated.description
    assert "Failed to roll back stage after dispatch spawn failure" not in caplog.text


@pytest.mark.asyncio
async def test_third_spawn_failure_escalates(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    """Third spawn failure escalates."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, stage_state="in_progress", dispatch_failure_count=2)
    action = SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go")

    async def fake_spawn_agent_impl(**_kwargs):
        return {"success": False, "error": "broken"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)

    updated = get_task(temp_db, task.id)
    assert updated.is_escalated is True
    assert updated.escalation_reason.startswith("dispatch_spawn_max_attempts:broken")


@pytest.mark.asyncio
async def test_spawn_prefers_project_scoped_git_manager(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path,
) -> None:
    """Spawn prefers project scoped git manager."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch.spawn import spawn_agent
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, isolation="worktree")
    TaskArtifactManager(temp_db).set_artifacts_atomic(task.id, target_branch="dev")

    default_repo = tmp_path / "default-repo"
    project_repo = tmp_path / "project-repo"
    default_repo.mkdir()
    project_repo.mkdir()
    default_git = SimpleNamespace(repo_path=str(default_repo))
    project_git = SimpleNamespace(repo_path=str(project_repo))
    default_clone = object()
    captured: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs):
        captured.update(kwargs)
        return {"success": True, "run_id": "run-project-git"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=SessionManager(temp_db),
        agent_runner=SimpleNamespace(),
        git_manager=default_git,
        clone_manager=default_clone,
        get_git_manager=lambda project_id: project_git,
    )

    run_id = await spawn_agent(
        SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go"),
        db=temp_db,
        services=services,
    )

    assert run_id == "run-project-git"
    assert captured["git_manager"] is project_git
    assert captured["clone_manager"] is not default_clone
    clone_manager = captured["clone_manager"]
    assert str(clone_manager.repo_path) == project_git.repo_path
    assert captured["base_branch"] == "dev"


@pytest.mark.asyncio
async def test_dispatch_spawn_uses_task_project_context_for_cross_project_build(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    tmp_path,
) -> None:
    """Dispatcher-spawned agents use the target task project, not the caller project."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch.spawn import spawn_agent
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    projects = LocalProjectManager(temp_db)
    caller_project = projects.create("caller-project", repo_path=str(tmp_path / "caller"))
    target_repo = tmp_path / "target"
    target_repo.mkdir()
    target_project = projects.create("target-project", repo_path=str(target_repo))
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=target_project.id,
        title="Target project task",
        task_type="task",
        category="code",
        allow_automation=True,
        isolation="none",
    )
    captured: dict[str, object] = {}

    async def fake_spawn_agent_impl(**kwargs):
        captured.update(kwargs)
        return {"success": True, "run_id": "run-target-project"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    sessions = SessionManager(temp_db)
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=sessions,
        agent_runner=SimpleNamespace(),
    )

    run_id = await spawn_agent(
        SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go"),
        db=temp_db,
        context=SimpleNamespace(project_id=caller_project.id),
        services=services,
    )
    launcher = sessions.get(str(captured["parent_session_id"]))

    assert run_id == "run-target-project"
    assert captured["project_path"] == str(target_repo)
    assert launcher is not None
    assert launcher.project_id == target_project.id


@pytest.mark.asyncio
async def test_bad_candidate_is_skipped_and_next_candidate_executes(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    """Bad candidate is skipped and next candidate executes."""
    from gobby.dispatch import dispatcher

    first = _task(temp_db, sample_project, "first")
    second = _task(temp_db, sample_project, "second")
    executed: list[str] = []

    def action_for(task, *_args):
        return _audit_action(task.id)

    async def flaky_execute(action, **kwargs):
        if action.task_id == first.id:
            raise RuntimeError("bad candidate")
        executed.append(action.task_id)
        return await dispatcher.append_audit_marker(
            kwargs["db"],
            action.task_id,
            action.heading,
            action.body,
        )

    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", action_for)
    monkeypatch.setattr(dispatcher, "execute_action", flaky_execute)

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 1
    assert result.skipped == 1
    assert executed == [second.id]
    assert "### Dispatch failed" in get_task(temp_db, first.id).description


@pytest.mark.asyncio
async def test_advance_action_releases_lease_immediately(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Advance action releases lease immediately."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, stage_name="development")
    storage = _mutex_storage(temp_db)
    action = StartStageAction(
        task_id=task.id,
        stage_name="development",
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert storage.get_mutex(task.id) is None


@pytest.mark.asyncio
async def test_start_pipeline_action_links_execution_id(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Start pipeline action links execution id."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, lifecycle="expanding")
    storage = _mutex_storage(temp_db)
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: _pipeline_action(task.id),
    )
    services = SimpleNamespace(pipeline_executor=_FakePipelineExecutor())

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)

    mutex = storage.get_mutex(task.id)
    assert mutex is not None
    assert mutex.run_id is not None
    assert mutex.action_kind == "stage-pipeline:expansion"


def test_dispatcher_run_heartbeat_cold_imports(repo_root) -> None:
    """Dispatcher run heartbeat cold imports."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from gobby.dispatch.dispatcher import run_heartbeat; print(run_heartbeat.__name__)",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "run_heartbeat"


def test_dispatch_inputs_invalid_json_logs_debug(
    caplog: pytest.LogCaptureFixture,
    enable_log_propagation: None,
) -> None:
    """Dispatch inputs invalid json logs debug."""
    from gobby.dispatch import rules

    registry_entry = SimpleNamespace(
        id="registry-1",
        stage_name="expansion",
        dispatch_inputs_json='{"invalid"',
    )

    with caplog.at_level(logging.DEBUG, logger="gobby.dispatch.rules"):
        assert rules._dispatch_inputs(registry_entry) == {}

    records = [
        record
        for record in caplog.records
        if record.message == "Invalid stage registry dispatch_inputs_json; ignoring"
    ]
    assert len(records) == 1
    assert records[0].registry_entry == {"id": "registry-1", "stage_name": "expansion"}
    assert records[0].raw_dispatch_inputs_json == '{"invalid"'
    assert "Expecting" in records[0].error


def test_build_context_loads_stage_registry_and_bundled_agents(temp_db, sample_project) -> None:
    """Build context loads stage registry and bundled agents."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher

    sync_bundled_agents(temp_db)
    task = _task(temp_db, sample_project, stage_name="pr", stage_state="in_progress")

    context = dispatcher.build_context(temp_db, task)

    assert context.stage_registry["pr"].default_agent == "merge-orchestrator"
    assert context.agents["merge-orchestrator"].enabled is True
    assert context.agent_definitions["merge-orchestrator"].spawn_capable is True


def test_build_context_project_disabled_agent_override_wins(temp_db, sample_project) -> None:
    """Build context project disabled agent override wins."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher
    from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
    from gobby.workflows.definitions import AgentDefinitionBody

    sync_bundled_agents(temp_db)
    LocalWorkflowDefinitionManager(temp_db).create(
        name="merge-orchestrator",
        workflow_type="agent",
        project_id=sample_project["id"],
        source="project",
        enabled=False,
        definition_json=AgentDefinitionBody(
            name="merge-orchestrator",
            description="Project override",
            surfaces=["spawn"],
        ).model_dump_json(),
    )
    task = _task(temp_db, sample_project, stage_name="pr", stage_state="in_progress")

    context = dispatcher.build_context(temp_db, task)

    assert context.agents["merge-orchestrator"].enabled is False
    assert context.agents["merge-orchestrator"].project_id == sample_project["id"]


@pytest.mark.asyncio
async def test_real_heartbeat_pr_stage_spawns_merge_orchestrator_without_false_no_agent(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Real heartbeat pr stage spawns merge orchestrator without false no agent."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher

    sync_bundled_agents(temp_db)
    task = _task(temp_db, sample_project, stage_name="pr", stage_state="in_progress")
    spawned: list[str] = []
    monkeypatch.setattr(
        dispatcher,
        "spawn_agent",
        lambda action, **kwargs: spawned.append(action.agent_slug) or "run-pr",
    )

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 1
    assert spawned == ["merge-orchestrator"]
    assert get_task(temp_db, task.id).is_escalated is False


@pytest.mark.asyncio
async def test_real_heartbeat_merge_ready_starts_then_spawns_merge_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Real heartbeat merge ready starts then spawns merge orchestrator."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher

    sync_bundled_agents(temp_db)
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Merge ready",
        task_type="feature",
        category="code",
    )
    update_task(temp_db, task.id, allow_automation=True, isolation="none")
    initialize_manifest(temp_db, task.id, [spec("pr", 0), spec("merge", 1)])
    set_stage_state(temp_db, task.id, "pr", "done")
    set_stage_state(temp_db, task.id, "merge", "ready")
    spawned: list[str] = []
    monkeypatch.setattr(
        dispatcher,
        "spawn_agent",
        lambda action, **kwargs: spawned.append(action.agent_slug) or "run-merge",
    )

    first = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    second = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert first.executed == 1
    assert second.executed == 1
    assert manager.stage_states.get(task.id, "merge").state == "in_progress"
    assert spawned == ["merge-orchestrator"]


@pytest.mark.asyncio
async def test_dispatcher_starts_stage_pipeline_with_injected_services(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Dispatcher starts stage pipeline with injected services."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, lifecycle="expanding")
    _session(temp_db, sample_project, "session-1")
    executor = _FakePipelineExecutor()
    services = SimpleNamespace(
        pipeline_executor=executor,
        triggering_session_id="session-1",
    )
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: _pipeline_action(task.id),
    )

    async def record_background(*args, **kwargs):
        executor.record_call(
            {
                "inputs": args[2],
                "execution_id": args[4],
                "session_id": kwargs.get("session_id"),
            }
        )

    monkeypatch.setattr(dispatcher, "_execute_pipeline_background", record_background)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)
    calls = await _wait_for_executor_calls(executor)

    assert calls[0]["inputs"] == {"task_id": task.id}
    assert calls[0]["session_id"] == "session-1"


@pytest.mark.asyncio
async def test_expansion_terminal_event_releases_lease_via_handlers(
    temp_db, sample_project
) -> None:
    """Expansion terminal event releases lease via handlers."""
    from gobby.hooks.event_handlers import _dispatch

    task = _task(temp_db, sample_project)
    storage = _mutex_storage(temp_db)
    storage.acquire_mutex(task.id, holder="dispatcher", kind="expansion", ttl_seconds=30)
    storage.attach_run_id(task.id, "expansion-1")

    _dispatch.on_expansion_run_cancelled(task.id, "expansion-1", storage=storage)

    assert storage.get_mutex(task.id) is None


@pytest.mark.asyncio
async def test_execution_id_attaches_before_background_pipeline_start(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Execution id attaches before background pipeline start."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, lifecycle="expanding")
    storage = _mutex_storage(temp_db)
    executor = _FakePipelineExecutor()
    services = SimpleNamespace(pipeline_executor=executor)
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: _pipeline_action(task.id),
    )

    async def record_background(*args, **kwargs):
        executor.record_call({"execution_id": args[4]})

    monkeypatch.setattr(dispatcher, "_execute_pipeline_background", record_background)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)
    calls = await _wait_for_executor_calls(executor)

    execution_id = calls[0]["execution_id"]
    assert storage.get_mutex(task.id).run_id == execution_id


@pytest.mark.asyncio
async def test_pipeline_terminal_handler_releases_lease(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Pipeline terminal handler releases lease."""
    from gobby.dispatch import dispatcher
    from gobby.hooks.event_handlers import _dispatch

    task = _task(temp_db, sample_project, lifecycle="expanding")
    storage = _mutex_storage(temp_db)
    executor = _FakePipelineExecutor()
    services = SimpleNamespace(pipeline_executor=executor)
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: _pipeline_action(task.id),
    )

    async def record_background(*args, **kwargs):
        executor.record_call({"execution_id": args[4]})

    monkeypatch.setattr(dispatcher, "_execute_pipeline_background", record_background)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)
    calls = await _wait_for_executor_calls(executor)
    _dispatch.on_pipeline_failed(
        {"execution_id": calls[0]["execution_id"], "error": "boom"},
        db=temp_db,
        storage=storage,
    )

    assert storage.get_mutex(task.id) is None


def test_terminal_handler_release_by_task_id_fallback(temp_db, sample_project) -> None:
    """Terminal handler release by task id fallback."""
    from gobby.hooks.event_handlers import _dispatch

    task = _task(temp_db, sample_project)
    storage = _mutex_storage(temp_db)
    storage.acquire_mutex(task.id, holder="dispatcher", kind="spawn", ttl_seconds=30)

    released = _dispatch.on_agent_terminal({"task_id": task.id}, storage=storage)

    assert released == 1
    assert storage.get_mutex(task.id) is None


@pytest.mark.asyncio
async def test_invalid_pipeline_target_escalates_and_releases(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Invalid pipeline target escalates and releases."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, lifecycle="expanding")
    storage = _mutex_storage(temp_db)
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: StartPipelineAction(
            task_id=task.id,
            task_ref="#1",
            stage_name="expansion",
            pipeline_name="missing",
            dispatch_inputs={},
        ),
    )
    services = SimpleNamespace(pipeline_executor=_FakePipelineExecutor())

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)

    assert storage.get_mutex(task.id) is None
    assert get_task(temp_db, task.id).is_escalated is True


@pytest.mark.asyncio
async def test_stage_pipeline_runtime_mutex_failure_is_retry_neutral(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Internal stage-pipeline mutex races restore the stage without burning attempts."""
    from gobby.dispatch import dispatcher
    from gobby.dispatch.mutex import RuntimeDispatchMutexError
    from tests.storage.tasks._stage_test_helpers import lifecycle_events, stage_row

    task = _task(
        temp_db,
        sample_project,
        lifecycle="expanding",
        stage_state="in_progress",
        isolation="worktree",
    )
    set_stage_state(temp_db, task.id, "expansion", "in_progress", work_attempt_count=1)
    storage = _mutex_storage(temp_db)
    services = SimpleNamespace(pipeline_executor=_FakePipelineExecutor())
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: _pipeline_action(task.id),
    )

    def fail_attach(*_args: object, **_kwargs: object) -> str:
        raise RuntimeDispatchMutexError(
            f"dispatch mutex for task {task.id!r} is held by another dispatcher"
        )

    monkeypatch.setattr(dispatcher, "_create_stage_pipeline_execution", fail_attach)

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=services,
    )

    row = stage_row(temp_db, task.id, "expansion")
    updated = get_task(temp_db, task.id)
    assert result.executed == 1
    assert storage.get_mutex(task.id) is None
    assert row["state"] == "ready"
    assert row["work_attempt_count"] == 0
    assert updated.is_escalated is False
    assert updated.dispatch_failure_count == 0
    assert lifecycle_events(temp_db, task.id)[-1]["reason"].startswith(
        "stage_pipeline_dispatch_retry_neutral:"
    )


@pytest.mark.asyncio
async def test_create_isolation_action_writes_artifact_pair_and_base_commit_sha_atomically(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Create isolation action writes artifact pair and base commit sha atomically."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, isolation="worktree")
    TaskArtifactManager(temp_db).set_artifacts_atomic(task.id, target_branch="main")
    monkeypatch.setattr(dispatcher, "resolve_branch_sha", lambda branch: "abc123")
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: CreateIsolationAction(
            task_id=task.id,
            task_ref="#1",
            isolation="worktree",
        ),
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert artifacts.worktree_path
    assert artifacts.worktree_id
    assert artifacts.base_commit_sha == "abc123"


@pytest.mark.asyncio
async def test_create_isolation_action_resolves_base_commit_sha_from_target_branch(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Create isolation action resolves base commit sha from target branch."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, isolation="worktree")
    TaskArtifactManager(temp_db).set_artifacts_atomic(task.id, target_branch="main")
    resolved: list[str] = []
    monkeypatch.setattr(
        dispatcher, "resolve_branch_sha", lambda branch: resolved.append(branch) or "abc123"
    )
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: CreateIsolationAction(task.id, "#1", "worktree"),
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert resolved == ["main"]


def test_persist_spawn_artifacts_writes_base_commit_sha(
    temp_db,
    sample_project,
) -> None:
    """Persist spawn artifacts writes base commit sha."""
    from gobby.dispatch.spawn import _persist_spawn_artifacts

    task = _task(temp_db, sample_project, isolation="worktree")

    _persist_spawn_artifacts(
        temp_db,
        task.id,
        {
            "worktree_id": "wt-1",
            "worktree_path": "/tmp/worktree",
            "base_commit_sha": "base-sha",
        },
    )

    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert artifacts.worktree_id == "wt-1"
    assert artifacts.worktree_path == "/tmp/worktree"
    assert artifacts.base_commit_sha == "base-sha"

    clone_task = _task(temp_db, sample_project, isolation="clone", title="Clone dispatch task")
    _persist_spawn_artifacts(
        temp_db,
        clone_task.id,
        {
            "clone_id": "clone-1",
            "clone_path": "/tmp/clone",
            "base_commit_sha": "clone-base",
        },
    )

    clone_artifacts = TaskArtifactManager(temp_db).get_artifacts(clone_task.id)
    assert clone_artifacts.clone_id == "clone-1"
    assert clone_artifacts.clone_path == "/tmp/clone"
    assert clone_artifacts.base_commit_sha == "clone-base"


def test_persist_spawn_artifacts_updates_standalone_base_commit_sha(
    temp_db,
    sample_project,
) -> None:
    """A spawn result may only refresh the base SHA for an existing workspace."""
    from gobby.dispatch.spawn import _persist_spawn_artifacts

    task = _task(temp_db, sample_project, isolation="worktree")
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_id="wt-1",
        worktree_path="/tmp/worktree",
        base_commit_sha="old-base",
    )

    _persist_spawn_artifacts(temp_db, task.id, {"base_commit_sha": "new-base"})

    artifacts = TaskArtifactManager(temp_db).get_artifacts(task.id)
    assert artifacts.worktree_id == "wt-1"
    assert artifacts.worktree_path == "/tmp/worktree"
    assert artifacts.base_commit_sha == "new-base"


@pytest.mark.asyncio
async def test_dispatch_spawn_tolerates_build_coordinator_subscription_failure(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Agent spawn succeeds even when best-effort coordinator subscription fails."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch.spawn import spawn_agent
    from gobby.storage.build_history import BuildHistoryStorage
    from gobby.storage.sessions import SessionManager

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    task = _task(temp_db, sample_project, isolation="none")
    sessions = SessionManager(temp_db)
    coordinator = sessions.register(
        external_id="coordinator-subscribe-failure",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    BuildHistoryStorage(temp_db).record_run(
        project_id=sample_project["id"],
        root_task_id=task.id,
        input_ref=f"#{task.seq_num}",
        action="build",
        summary={"coordinator_session_id": coordinator.id},
    )

    async def fake_spawn_agent_impl(**_kwargs):
        return {"success": True, "run_id": "run-subscribe-failure"}

    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )
    monkeypatch.setattr(
        "gobby.dispatch.spawn.subscribe_agent_completion",
        MagicMock(side_effect=RuntimeError("subscriber store unavailable")),
    )
    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=sessions,
        agent_runner=SimpleNamespace(),
    )

    run_id = await spawn_agent(
        SpawnAgentAction(task.id, f"#{task.seq_num}", "backend-developer", "go"),
        db=temp_db,
        services=services,
    )

    assert run_id == "run-subscribe-failure"


@pytest.mark.asyncio
async def test_create_isolation_action_missing_target_branch_escalates(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Create isolation action missing target branch escalates."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, isolation="worktree")
    escalations: list[dict[str, object]] = []
    monkeypatch.setattr(dispatcher, "escalate_task", lambda **kwargs: escalations.append(kwargs))
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: CreateIsolationAction(task.id, "#1", "worktree"),
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert escalations[0]["reason"] == "isolation_missing_target_branch"


@pytest.mark.asyncio
async def test_dev_rule_fires_after_isolation_and_stage_start(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    """Dev rule fires after isolation and stage start."""
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher

    sync_bundled_agents(temp_db)
    task = _task(temp_db, sample_project, isolation="worktree")
    TaskArtifactManager(temp_db).set_artifacts_atomic(task.id, target_branch="main")
    spawned: list[str] = []
    monkeypatch.setattr(dispatcher, "resolve_branch_sha", lambda branch: "abc123")
    monkeypatch.setattr(
        dispatcher,
        "spawn_agent",
        lambda action, **kwargs: spawned.append(action.task_id) or "run-1",
    )

    first = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    second = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert first.executed == 1
    assert second.executed == 1
    assert spawned == [task.id]


@pytest.mark.asyncio
async def test_startup_sweep_clears_expired_leases(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Startup sweep clears expired leases."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, allow_automation=False)
    storage = _mutex_storage(temp_db)
    past = datetime.now(UTC) - timedelta(seconds=60)
    storage.acquire_mutex(task.id, holder="old", kind="test", ttl_seconds=1, now=past)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], startup=True)

    assert storage.get_mutex(task.id) is None


@pytest.mark.asyncio
async def test_startup_sweep_preserves_expired_leases_for_active_runs(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Startup sweep preserves expired mutexes that still belong to active runs."""
    from gobby.dispatch import dispatcher
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SYSTEM_SESSION_ID

    active_task = _task(temp_db, sample_project, "Active", allow_automation=False)
    orphan_task = _task(temp_db, sample_project, "Orphan", allow_automation=False)
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=SYSTEM_SESSION_ID,
        provider="codex",
        prompt="work",
        task_id=active_task.id,
    )
    storage = _mutex_storage(temp_db)
    past = datetime.now(UTC) - timedelta(seconds=60)
    storage.acquire_mutex(
        active_task.id,
        holder="old",
        kind="test",
        ttl_seconds=1,
        run_id=run.id,
        now=past,
    )
    storage.acquire_mutex(
        orphan_task.id,
        holder="old",
        kind="test",
        ttl_seconds=1,
        now=past,
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], startup=True)

    assert storage.get_mutex(active_task.id) is not None
    assert storage.get_mutex(orphan_task.id) is None


@pytest.mark.asyncio
async def test_heartbeat_preserves_no_run_mutex_with_live_lease(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Heartbeat preserves no-run mutexes while their lease is still active."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    old_acquired_at = datetime.now(UTC) - timedelta(
        seconds=dispatcher.ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS + 5
    )
    assert storage.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=dispatcher.DISPATCH_TTL_SECONDS,
        now=old_acquired_at,
    )
    spawned: list[str] = []
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: SpawnAgentAction(
            task_id=task.id,
            task_ref=f"#{task.seq_num}",
            agent_slug="backend-developer",
            prompt="resume work",
        ),
    )
    monkeypatch.setattr(
        dispatcher,
        "spawn_agent",
        lambda action, **kwargs: spawned.append(action.task_id) or "run-duplicate",
    )

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 0
    assert result.skipped == 0
    assert spawned == []
    mutex = storage.get_mutex(task.id)
    assert mutex is not None
    assert mutex.run_id is None


@pytest.mark.asyncio
async def test_heartbeat_recovers_expired_no_run_mutex(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Heartbeat recovers no-run mutexes after their lease expires."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    old_acquired_at = datetime.now(UTC) - timedelta(
        seconds=dispatcher.ORPHAN_NO_RUN_MUTEX_GRACE_SECONDS + 5
    )
    assert storage.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=1,
        now=old_acquired_at,
    )
    spawned: list[str] = []
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: SpawnAgentAction(
            task_id=task.id,
            task_ref=f"#{task.seq_num}",
            agent_slug="backend-developer",
            prompt="resume work",
        ),
    )
    monkeypatch.setattr(
        dispatcher,
        "spawn_agent",
        lambda action, **kwargs: spawned.append(action.task_id) or "run-recovered",
    )

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 1
    assert spawned == [task.id]
    mutex = storage.get_mutex(task.id)
    assert mutex is not None
    assert mutex.run_id == "run-recovered"


@pytest.mark.asyncio
async def test_heartbeat_preserves_fresh_no_run_mutex(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Heartbeat preserves fresh no run mutex."""
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, stage_state="in_progress")
    storage = _mutex_storage(temp_db)
    assert storage.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=dispatcher.DISPATCH_TTL_SECONDS,
        now=datetime.now(UTC),
    )
    spawned: list[str] = []
    monkeypatch.setattr(
        dispatcher,
        "spawn_agent",
        lambda action, **kwargs: spawned.append(action.task_id) or "run-too-soon",
    )

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 0
    assert spawned == []
    mutex = storage.get_mutex(task.id)
    assert mutex is not None
    assert mutex.run_id is None

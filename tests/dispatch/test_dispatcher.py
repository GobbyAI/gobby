"""Dispatcher heartbeat scanner tests."""

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from gobby.dispatch.actions import (
    AppendAuditMarkerAction,
    CreateIsolationAction,
    SpawnAgentAction,
    StartPipelineAction,
    StartStageAction,
)
from gobby.storage.tasks import LocalTaskManager
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
    "test_arch": "test_arch",
}


def _task(temp_db, sample_project, title: str = "Dispatch task", **fields):
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
            "UPDATE tasks SET closed_at = ?, closed_reason = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), "test_terminal", task.id),
        )
    return get_task(temp_db, task.id)


def _mutex_storage(temp_db) -> TaskDispatchMutexManager:
    storage = TaskDispatchMutexManager(temp_db)
    storage.ensure_table()
    return storage


def _session(temp_db, sample_project, session_id: str = "session-1") -> str:
    temp_db.execute(
        """
        INSERT INTO sessions (id, external_id, machine_id, source, project_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (session_id, session_id, "machine-1", "test", sample_project["id"]),
    )
    return session_id


def _audit_action(task_id: str) -> AppendAuditMarkerAction:
    return AppendAuditMarkerAction(task_id=task_id, heading="Dispatch", body="marker")


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

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=kwargs["execution_id"], status="completed")


def _pipeline_action(task_id: str) -> StartPipelineAction:
    return StartPipelineAction(
        task_id=task_id,
        task_ref="#1",
        stage_name="expansion",
        pipeline_name="expand-task",
        dispatch_inputs={"task_id": "${{ task_id }}"},
    )


def test_candidate_filter_excludes_claimed_leased_blocked_terminal(temp_db, sample_project) -> None:
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
        "UPDATE tasks SET closed_at = ? WHERE id = ?",
        (datetime.now(UTC).isoformat(), terminal.id),
    )
    temp_db.execute(
        """
        INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at)
        VALUES (?, ?, 'blocks', ?)
        """,
        (blocked.id, blocker.id, datetime.now(UTC).isoformat()),
    )

    candidates = _crud.list_automation_candidates(temp_db, project_id=sample_project["id"])

    assert [candidate.id for candidate in candidates] == [ready.id]
    assert not _crud.is_blocked_by_deps(candidates[0])


def test_count_active_agents_scopes_by_parent_session_project(temp_db, sample_project) -> None:
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


async def test_max_active_agents_cap(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
    from gobby.dispatch import dispatcher

    _task(temp_db, sample_project)
    spawned: list[object] = []
    monkeypatch.setattr(dispatcher, "count_active_agents", lambda *args, **kwargs: 2)
    monkeypatch.setattr(dispatcher, "MAX_ACTIVE_AGENTS", 2)
    monkeypatch.setattr(dispatcher, "spawn_agent", lambda *args, **kwargs: spawned.append(args))

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.cap_reached is True
    assert spawned == []


async def test_mutex_lifecycle(monkeypatch: pytest.MonkeyPatch, temp_db, sample_project) -> None:
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


async def test_toctou_skip_on_changed_tuple(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
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


async def test_first_match_action_executed(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
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


async def test_spawn_action_links_run_id(
    monkeypatch: pytest.MonkeyPatch, temp_db, sample_project
) -> None:
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


async def test_advance_action_releases_lease_immediately(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, stage_name="test_arch")
    storage = _mutex_storage(temp_db)
    action = StartStageAction(
        task_id=task.id,
        stage_name="test_arch",
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert storage.get_mutex(task.id) is None


async def test_start_pipeline_action_links_execution_id(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
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


def test_dispatcher_run_heartbeat_cold_imports() -> None:
    repo_root = Path(__file__).resolve().parents[2]

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


def test_build_context_loads_stage_registry_and_bundled_agents(temp_db, sample_project) -> None:
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher

    sync_bundled_agents(temp_db)
    task = _task(temp_db, sample_project, stage_name="pr", stage_state="in_progress")

    context = dispatcher.build_context(temp_db, task)

    assert context.stage_registry["pr"].default_agent == "merge-orchestrator"
    assert context.agents["merge-orchestrator"].enabled is True
    assert context.agent_definitions["merge-orchestrator"].spawn_capable is True


def test_build_context_project_disabled_agent_override_wins(temp_db, sample_project) -> None:
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


async def test_real_heartbeat_pr_stage_spawns_merge_orchestrator_without_false_no_agent(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
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


async def test_real_heartbeat_merge_ready_starts_then_spawns_merge_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
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


async def test_dispatcher_starts_stage_pipeline_with_injected_services(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
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
        executor.calls.append(
            {
                "inputs": args[2],
                "execution_id": args[4],
                "session_id": kwargs.get("session_id"),
            }
        )

    monkeypatch.setattr(dispatcher, "_execute_pipeline_background", record_background)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)
    for _ in range(5):
        if executor.calls:
            break
        await asyncio.sleep(0.01)

    assert executor.calls[0]["inputs"] == {"task_id": task.id}
    assert executor.calls[0]["session_id"] == "session-1"


async def test_expansion_terminal_event_releases_lease_via_handlers(
    temp_db, sample_project
) -> None:
    from gobby.hooks.event_handlers import _dispatch

    task = _task(temp_db, sample_project)
    storage = _mutex_storage(temp_db)
    storage.acquire_mutex(task.id, holder="dispatcher", kind="expansion", ttl_seconds=30)
    storage.attach_run_id(task.id, "expansion-1")

    _dispatch.on_expansion_run_cancelled(task.id, "expansion-1", storage=storage)

    assert storage.get_mutex(task.id) is None


async def test_execution_id_attaches_before_background_pipeline_start(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
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
        executor.calls.append({"execution_id": args[4]})

    monkeypatch.setattr(dispatcher, "_execute_pipeline_background", record_background)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)
    await asyncio.sleep(0)

    execution_id = executor.calls[0]["execution_id"]
    assert storage.get_mutex(task.id).run_id == execution_id


async def test_pipeline_terminal_handler_releases_lease(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
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
        executor.calls.append({"execution_id": args[4]})

    monkeypatch.setattr(dispatcher, "_execute_pipeline_background", record_background)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)
    await asyncio.sleep(0)
    _dispatch.on_pipeline_failed(
        {"execution_id": executor.calls[0]["execution_id"], "error": "boom"},
        db=temp_db,
        storage=storage,
    )

    assert storage.get_mutex(task.id) is None


def test_terminal_handler_release_by_task_id_fallback(temp_db, sample_project) -> None:
    from gobby.hooks.event_handlers import _dispatch

    task = _task(temp_db, sample_project)
    storage = _mutex_storage(temp_db)
    storage.acquire_mutex(task.id, holder="dispatcher", kind="spawn", ttl_seconds=30)

    released = _dispatch.on_agent_terminal({"task_id": task.id}, storage=storage)

    assert released == 1
    assert storage.get_mutex(task.id) is None


async def test_invalid_pipeline_target_escalates_and_releases(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
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


async def test_create_isolation_action_writes_artifact_pair_and_base_commit_sha_atomically(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
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


async def test_create_isolation_action_resolves_base_commit_sha_from_target_branch(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
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


async def test_create_isolation_action_missing_target_branch_escalates(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
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


async def test_dev_rule_fires_after_isolation_and_stage_start(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.dispatch import dispatcher

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
    third = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert first.executed == 1
    assert second.executed == 1
    assert third.executed == 1
    assert spawned == [task.id]


async def test_startup_sweep_clears_expired_leases(temp_db, sample_project) -> None:
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, allow_automation=False)
    storage = _mutex_storage(temp_db)
    past = datetime.now(UTC) - timedelta(seconds=60)
    storage.acquire_mutex(task.id, holder="old", kind="test", ttl_seconds=1, now=past)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], startup=True)

    assert storage.get_mutex(task.id) is None

"""Dispatcher heartbeat scanner tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from gobby.dispatch.actions import (
    AdvanceLifecycleAction,
    AppendAuditMarkerAction,
    CreateIsolationAction,
    SpawnAgentAction,
    StartExpansionAction,
)
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._artifacts import TaskArtifactManager
from gobby.storage.tasks._crud import get_task, update_task
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

pytestmark = pytest.mark.unit


def _task(temp_db, sample_project, title: str = "Dispatch task", **fields):
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title=title)
    update_task(
        temp_db,
        task.id,
        allow_automation=fields.pop("allow_automation", True),
        lifecycle=fields.pop("lifecycle", "in_development"),
        status=fields.pop("status", "open"),
        task_type=fields.pop("task_type", "task"),
        assigned_agent=fields.pop("assigned_agent", "backend-developer"),
        isolation=fields.pop("isolation", "none"),
        claimed_by_session_id=fields.pop("claimed_by_session_id", None),
        **fields,
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
    update_task(temp_db, task_id, lifecycle="holistic_review", status="open")
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

    task = _task(temp_db, sample_project, lifecycle="test_arch")
    storage = _mutex_storage(temp_db)
    action = AdvanceLifecycleAction(
        task_id=task.id,
        from_lifecycle="test_arch",
        from_status="open",
        to_lifecycle="expanding",
        to_status="open",
        reason="skip",
    )
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert storage.get_mutex(task.id) is None


async def test_start_expansion_action_links_run_id(
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
        lambda *args, **kwargs: StartExpansionAction(task_id=task.id, task_ref="#1"),
    )
    monkeypatch.setattr(dispatcher, "allocate_expansion_run_id", lambda: "expansion-1")
    monkeypatch.setattr(dispatcher, "start_expansion_run_impl", lambda **kwargs: kwargs)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert storage.get_mutex(task.id).run_id == "expansion-1"


def test_dispatcher_imports_mcp_expansion_impl_directly() -> None:
    from gobby.dispatch import dispatcher
    from gobby.mcp_proxy.tools.tasks import _expansion

    assert dispatcher.start_expansion_run_impl is _expansion.start_expansion_run_impl


async def test_dispatcher_starts_expansion_with_injected_services(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, lifecycle="expanding")
    calls: list[dict[str, object]] = []
    services = SimpleNamespace(
        task_manager="task-manager",
        llm_service="llm-service",
        config="config",
        completion_registry="completion-registry",
        triggering_session_id="session-1",
        project="project-1",
    )
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: StartExpansionAction(task_id=task.id, task_ref="#1"),
    )
    monkeypatch.setattr(dispatcher, "allocate_expansion_run_id", lambda: "expansion-1")
    monkeypatch.setattr(
        dispatcher, "start_expansion_run_impl", lambda **kwargs: calls.append(kwargs)
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], services=services)

    assert calls == [
        {
            "task_id": task.id,
            "run_id": "expansion-1",
            "auto_apply": True,
            "task_manager": "task-manager",
            "llm_service": "llm-service",
            "config": "config",
            "completion_registry": "completion-registry",
            "triggering_session_id": "session-1",
            "project": "project-1",
        }
    ]


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


async def test_attach_run_id_precedes_start_expansion_run_impl(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, lifecycle="expanding")
    storage = _mutex_storage(temp_db)
    events: list[str] = []
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: StartExpansionAction(task_id=task.id, task_ref="#1"),
    )
    monkeypatch.setattr(dispatcher, "allocate_expansion_run_id", lambda: "expansion-1")

    def start(**kwargs):
        assert storage.get_mutex(task.id).run_id == "expansion-1"
        events.append("start")

    monkeypatch.setattr(dispatcher, "start_expansion_run_impl", start)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert events == ["start"]


async def test_synchronous_terminal_expansion_releases_lease(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.dispatch import dispatcher
    from gobby.hooks.event_handlers import _dispatch

    task = _task(temp_db, sample_project, lifecycle="expanding")
    storage = _mutex_storage(temp_db)
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: StartExpansionAction(task_id=task.id, task_ref="#1"),
    )
    monkeypatch.setattr(dispatcher, "allocate_expansion_run_id", lambda: "expansion-1")
    monkeypatch.setattr(
        dispatcher,
        "start_expansion_run_impl",
        lambda **kwargs: _dispatch.on_expansion_run_cancelled(
            task.id,
            "expansion-1",
            storage=storage,
        ),
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert storage.get_mutex(task.id) is None


def test_terminal_handler_release_by_task_id_fallback(temp_db, sample_project) -> None:
    from gobby.hooks.event_handlers import _dispatch

    task = _task(temp_db, sample_project)
    storage = _mutex_storage(temp_db)
    storage.acquire_mutex(task.id, holder="dispatcher", kind="spawn", ttl_seconds=30)

    released = _dispatch.on_agent_terminal({"task_id": task.id}, storage=storage)

    assert released == 1
    assert storage.get_mutex(task.id) is None


async def test_dispatcher_pins_auto_apply_true_on_start_expansion(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, lifecycle="expanding")
    configs: list[dict[str, object]] = []
    monkeypatch.setattr(
        dispatcher.dispatch_rules,
        "evaluate",
        lambda *args, **kwargs: StartExpansionAction(task_id=task.id, task_ref="#1"),
    )
    monkeypatch.setattr(
        dispatcher, "start_expansion_run_impl", lambda **kwargs: configs.append(kwargs)
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert configs[0]["auto_apply"] is True


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


async def test_dev_rule_fires_on_next_heartbeat_after_isolation_created(
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

    assert first.executed == 1
    assert second.executed == 1
    assert spawned == [task.id]


async def test_startup_sweep_clears_expired_leases(temp_db, sample_project) -> None:
    from gobby.dispatch import dispatcher

    task = _task(temp_db, sample_project, allow_automation=False)
    storage = _mutex_storage(temp_db)
    past = datetime.now(UTC) - timedelta(seconds=60)
    storage.acquire_mutex(task.id, holder="old", kind="test", ttl_seconds=1, now=past)

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"], startup=True)

    assert storage.get_mutex(task.id) is None

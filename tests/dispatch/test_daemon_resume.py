from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.agents.resume_executor import ResumeAgentResult
from gobby.agents.session import ChildSessionManager
from gobby.dispatch.actions import SpawnAgentAction
from gobby.storage.agents import AgentRunTerminalReason, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._crud import get_task, update_task
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from tests.storage.tasks._stage_test_helpers import initialize_manifest, set_stage_state, spec

pytestmark = pytest.mark.unit


def _task(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    *,
    work_attempt_count: int = 0,
) -> Task:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title="Daemon resume task")
    update_task(
        temp_db,
        task.id,
        allow_automation=True,
        task_type="task",
        assigned_agent="backend-developer",
        isolation="worktree",
    )
    initialize_manifest(temp_db, task.id, [spec("development", 0)])
    set_stage_state(
        temp_db,
        task.id,
        "development",
        "in_progress",
        work_attempt_count=work_attempt_count,
    )
    return get_task(temp_db, task.id)


def _workspace(tmp_path: Path, *, dirty: bool) -> Path:
    path = tmp_path / ("dirty" if dirty else "clean")
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    if dirty:
        (path / "work.txt").write_text("in progress\n")
    return path


def _services(temp_db: HubDatabase) -> SimpleNamespace:
    session_manager = SessionManager(temp_db)
    run_storage = LocalAgentRunManager(temp_db)
    return SimpleNamespace(
        database=temp_db,
        task_manager=LocalTaskManager(temp_db),
        session_manager=session_manager,
        agent_runner=SimpleNamespace(
            child_session_manager=ChildSessionManager(session_manager, max_agent_depth=5),
            run_storage=run_storage,
        ),
    )


def _seed_daemon_stop_run(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    *,
    task_id: str,
    workspace: Path,
    terminal_reason: AgentRunTerminalReason | None = "daemon_stop",
) -> None:
    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id=f"parent-{task_id[:8]}",
        machine_id="machine-1",
        source="dispatcher",
        project_id=sample_project["id"],
    )
    child = sessions.register(
        external_id="native-session-123",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
        parent_session_id=parent.id,
        agent_depth=1,
    )
    runs = LocalAgentRunManager(temp_db)
    run = runs.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        provider="codex",
        prompt="work",
        agent_name="backend-developer",
        task_id=task_id,
        run_id=f"run-original-{task_id[:4]}",
        resume_metadata_json={
            "provider": "codex",
            "model": "gpt-5",
            "effective_reasoning_effort": "high",
            "cwd": str(workspace),
            "workspace_path": str(workspace),
            "project_id": sample_project["id"],
            "project_path": str(workspace),
            "parent_session_id": parent.id,
            "agent_slug": "backend-developer",
            "stage_name": "development",
            "stage_state": "in_progress",
            "initial_variables": {
                "stage_name": "development",
                "stage_state": "in_progress",
            },
            "auto_approve": True,
        },
    )
    runs.cancel(run.id, terminal_reason=terminal_reason)


@pytest.mark.asyncio
async def test_dirty_daemon_stop_workspace_resumes_before_fresh_spawn(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.dispatch import daemon_resume, dispatcher

    task = _task(temp_db, sample_project)
    workspace = _workspace(tmp_path, dirty=True)
    _seed_daemon_stop_run(temp_db, sample_project, task_id=task.id, workspace=workspace)
    action = SpawnAgentAction(
        task.id,
        f"#{task.seq_num}",
        "backend-developer",
        "go",
        initial_variables={"stage_name": "development", "stage_state": "in_progress"},
    )
    captured: dict[str, object] = {}

    async def fake_resume_agent_run(original_run, **kwargs):
        captured["run_id"] = original_run.id
        captured["metadata"] = kwargs["resume_metadata"]
        return ResumeAgentResult(True, run_id="run-resumed")

    monkeypatch.setattr(daemon_resume, "resume_agent_run", fake_resume_agent_run)
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    monkeypatch.setattr(
        dispatcher,
        "spawn_agent",
        lambda *_args, **_kwargs: pytest.fail("fresh spawn should not run"),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=_services(temp_db),
    )

    mutex = TaskDispatchMutexManager(temp_db).get_mutex(task.id)
    assert result.executed == 1
    assert mutex is not None
    assert mutex.run_id == "run-resumed"
    assert captured["metadata"]["model"] == "gpt-5"  # type: ignore[index]


@pytest.mark.asyncio
async def test_dirty_daemon_stop_resume_failure_escalates_without_attempt_increment(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.dispatch import daemon_resume, dispatcher

    task = _task(temp_db, sample_project, work_attempt_count=2)
    workspace = _workspace(tmp_path, dirty=True)
    _seed_daemon_stop_run(temp_db, sample_project, task_id=task.id, workspace=workspace)
    action = SpawnAgentAction(
        task.id,
        f"#{task.seq_num}",
        "backend-developer",
        "go",
        initial_variables={"stage_name": "development", "stage_state": "in_progress"},
    )

    async def failed_resume(*_args, **_kwargs):
        return ResumeAgentResult(False, error="native resume failed")

    monkeypatch.setattr(daemon_resume, "resume_agent_run", failed_resume)
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    monkeypatch.setattr(
        dispatcher,
        "spawn_agent",
        lambda *_args, **_kwargs: pytest.fail("fresh spawn should not run"),
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=_services(temp_db),
    )

    updated = get_task(temp_db, task.id)
    stage = LocalTaskManager(temp_db).stage_states.get(task.id, "development")
    assert result.executed == 1
    assert TaskDispatchMutexManager(temp_db).get_mutex(task.id) is None
    assert updated.is_escalated is True
    assert updated.escalation_reason == "agent_resume_after_daemon_restart_failed"
    assert updated.dispatch_failure_count == 0
    assert "### Agent resume after daemon restart failed" in updated.description
    assert stage is not None
    assert stage.state == "in_progress"
    assert stage.work_attempt_count == 2


@pytest.mark.asyncio
async def test_clean_daemon_stop_resume_failure_allows_fresh_spawn(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.dispatch import daemon_resume, dispatcher

    task = _task(temp_db, sample_project)
    workspace = _workspace(tmp_path, dirty=False)
    _seed_daemon_stop_run(temp_db, sample_project, task_id=task.id, workspace=workspace)
    action = SpawnAgentAction(
        task.id,
        f"#{task.seq_num}",
        "backend-developer",
        "go",
        initial_variables={"stage_name": "development", "stage_state": "in_progress"},
    )
    spawned: list[str] = []

    async def failed_resume(*_args, **_kwargs):
        return ResumeAgentResult(False, error="native resume failed")

    monkeypatch.setattr(daemon_resume, "resume_agent_run", failed_resume)
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    monkeypatch.setattr(
        dispatcher,
        "spawn_agent",
        lambda action, **_kwargs: spawned.append(action.task_id) or "run-fresh",
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=_services(temp_db),
    )

    updated = get_task(temp_db, task.id)
    mutex = TaskDispatchMutexManager(temp_db).get_mutex(task.id)
    assert result.executed == 1
    assert spawned == [task.id]
    assert mutex is not None
    assert mutex.run_id == "run-fresh"
    assert updated.dispatch_failure_count == 0
    assert "### Agent resume after daemon restart failed" not in (updated.description or "")


@pytest.mark.asyncio
async def test_non_daemon_stop_dirty_workspace_uses_existing_spawn_policy(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.dispatch import daemon_resume, dispatcher

    task = _task(temp_db, sample_project)
    workspace = _workspace(tmp_path, dirty=True)
    _seed_daemon_stop_run(
        temp_db,
        sample_project,
        task_id=task.id,
        workspace=workspace,
        terminal_reason="user_cancelled",
    )
    action = SpawnAgentAction(
        task.id,
        f"#{task.seq_num}",
        "backend-developer",
        "go",
        initial_variables={"stage_name": "development", "stage_state": "in_progress"},
    )
    spawned: list[str] = []

    async def unexpected_resume(*_args, **_kwargs):
        raise AssertionError("non-daemon-stop run should not resume")

    monkeypatch.setattr(daemon_resume, "resume_agent_run", unexpected_resume)
    monkeypatch.setattr(dispatcher.dispatch_rules, "evaluate", lambda *args, **kwargs: action)
    monkeypatch.setattr(
        dispatcher,
        "spawn_agent",
        lambda action, **_kwargs: spawned.append(action.task_id) or "run-fresh",
    )

    result = await dispatcher.run_heartbeat(
        db=temp_db,
        project_id=sample_project["id"],
        services=_services(temp_db),
    )

    assert result.executed == 1
    assert spawned == [task.id]

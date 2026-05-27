from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _project(temp_db, name: str = "observability") -> str:
    from gobby.storage.projects import LocalProjectManager

    return LocalProjectManager(temp_db).create(name=name, repo_path=f"/tmp/{name}").id


def _automated_task(temp_db, project_id: str, title: str = "Task"):
    from gobby.storage.tasks import LocalTaskManager

    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=project_id, title=title, category="code")
    manager.initialize_task_manifest(task.id, stage_names=["development"])
    return manager.update_task(task.id, allow_automation=True, isolation="none")


def test_get_build_status_reports_agents_mutex_artifacts_events_and_comments(
    temp_db,
    tmp_path: Path,
) -> None:
    from gobby.build.observability import get_build_status
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SYSTEM_SESSION_ID
    from gobby.storage.tasks import LocalTaskManager, TaskArtifactManager
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
    from gobby.storage.tasks._lifecycle_events import BUILD_EVENT_REASON

    project_id = _project(temp_db)
    manager = LocalTaskManager(temp_db)
    task = _automated_task(temp_db, project_id)
    manager.lifecycle_events.record_lifecycle_event(
        task.id,
        from_state=None,
        to_state="development",
        reason=BUILD_EVENT_REASON,
        by_actor="build",
    )
    missing_path = tmp_path / "missing-worktree"
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        worktree_path=str(missing_path),
        worktree_id="wt-1",
        base_commit_sha="abc123",
    )
    LocalAgentRunManager(temp_db).create(
        parent_session_id=SYSTEM_SESSION_ID,
        provider="codex",
        prompt="work",
        agent_name="backend-developer",
        task_id=task.id,
    )
    TaskDispatchMutexManager(temp_db).acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=60,
    )
    temp_db.execute(
        """
        INSERT INTO task_comments (id, task_id, author, author_type, body)
        VALUES (?, ?, 'dispatcher', 'system', '## Holistic QA Failure\n\nNeeds work')
        """,
        ("comment-1", task.id),
    )

    status = get_build_status(
        f"#{task.seq_num}",
        db=temp_db,
        project_id=project_id,
        history_limit=5,
    )

    assert status["ok"] is True
    assert status["summary"]["state"] == "running"
    assert status["summary"]["active_agents"] == 1
    assert status["tasks"][0]["has_build_event"] is True
    assert status["tasks"][0]["latest_failure_comment"]["id"] == "comment-1"
    assert status["mutexes"][0]["state"] == "active_no_run"
    assert status["artifact_health"]["ok"] is False
    assert status["artifact_health"]["items"][0]["artifacts"][0]["exists"] is False
    assert status["recent_events"][0]["reason"] == BUILD_EVENT_REASON


def test_get_build_status_counts_closed_and_escalated_nodes(temp_db) -> None:
    from gobby.build.observability import get_build_status
    from gobby.storage.tasks import LocalTaskManager

    project_id = _project(temp_db, "observability-tree")
    manager = LocalTaskManager(temp_db)
    root = manager.create_task(project_id=project_id, title="Root", task_type="epic")
    closed = manager.create_task(project_id=project_id, title="Closed", parent_task_id=root.id)
    escalated = manager.create_task(
        project_id=project_id,
        title="Escalated",
        parent_task_id=root.id,
        category="code",
    )
    manager.close_task(closed.id, force=True)
    manager.escalate_task(escalated.id, "needs_human")

    status = get_build_status(f"#{root.seq_num}", db=temp_db, project_id=project_id)

    assert status["summary"]["total_tasks"] == 3
    assert status["summary"]["closed_tasks"] == 1
    assert status["summary"]["escalated_tasks"] == 1
    by_id = {task["task_id"]: task for task in status["tasks"]}
    assert by_id[closed.id]["closed"] is True
    assert by_id[escalated.id]["escalated"] is True


def test_get_build_status_reports_closed_root_as_completed(temp_db) -> None:
    from gobby.build.observability import get_build_status
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.tasks._lifecycle_events import BUILD_EVENT_REASON

    project_id = _project(temp_db, "observability-completed-root")
    manager = LocalTaskManager(temp_db)
    root = _automated_task(temp_db, project_id, "Completed Root")
    manager.lifecycle_events.record_lifecycle_event(
        root.id,
        from_state=None,
        to_state="development",
        reason=BUILD_EVENT_REASON,
        by_actor="build",
    )
    manager.close_task(root.id, force=True)

    status = get_build_status(f"#{root.seq_num}", db=temp_db, project_id=project_id)

    assert status["summary"]["state"] == "completed"
    assert status["summary"]["open_tasks"] == 0
    assert status["summary"]["closed_tasks"] == 1


def test_list_build_history_resolves_task_refs(temp_db) -> None:
    from gobby.build.observability import list_build_history
    from gobby.storage.build_history import BuildHistoryStorage

    project_id = _project(temp_db, "observability-history")
    task = _automated_task(temp_db, project_id)
    history = BuildHistoryStorage(temp_db)
    run = history.record_run(
        project_id=project_id,
        root_task_id=task.id,
        input_ref=f"#{task.seq_num}",
        action="build",
    )
    history.record_event(
        run_id=run.id,
        project_id=project_id,
        root_task_id=task.id,
        event_type="build_completed",
        action="build",
    )

    payload = list_build_history(f"#{task.seq_num}", db=temp_db, project_id=project_id)

    assert payload["root_task_id"] == task.id
    assert payload["runs"][0]["id"] == run.id
    assert payload["events"][0]["event_type"] == "build_completed"


def test_explain_dispatch_reports_block_reasons_and_would_dispatch(temp_db) -> None:
    from gobby.build.observability import explain_dispatch
    from gobby.storage.sessions import SYSTEM_SESSION_ID
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

    project_id = _project(temp_db, "observability-explain")
    manager = LocalTaskManager(temp_db)
    disabled = _automated_task(temp_db, project_id, "Disabled")
    manager.update_task(disabled.id, allow_automation=False)
    claimed = _automated_task(temp_db, project_id, "Claimed")
    manager.claim_task(claimed.id, SYSTEM_SESSION_ID)
    mutexed = _automated_task(temp_db, project_id, "Mutexed")
    TaskDispatchMutexManager(temp_db).acquire_mutex(
        mutexed.id,
        holder="dispatcher",
        kind="heartbeat",
        ttl_seconds=60,
    )
    blocked = _automated_task(temp_db, project_id, "Blocked")
    blocker = manager.create_task(project_id=project_id, title="Blocker")
    temp_db.execute(
        "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (?, ?, 'blocks', NOW())",
        (blocked.id, blocker.id),
    )
    parent = manager.create_task(project_id=project_id, title="Parent", task_type="epic")
    manager.update_task(parent.id, allow_automation=False, isolation="none")
    manager.initialize_task_manifest(
        parent.id, stage_names=["planning", "expansion", "development"]
    )
    temp_db.execute(
        "UPDATE task_stage_states SET state = 'done' WHERE task_id = ? AND stage_name = 'planning'",
        (parent.id,),
    )
    temp_db.execute(
        "UPDATE task_stage_states SET state = 'needs_review' WHERE task_id = ? AND stage_name = 'expansion'",
        (parent.id,),
    )
    ancestor_blocked = manager.create_task(
        project_id=project_id,
        title="Ancestor blocked",
        category="code",
        parent_task_id=parent.id,
    )
    manager.initialize_task_manifest(ancestor_blocked.id, stage_names=["development"])
    manager.update_task(ancestor_blocked.id, allow_automation=True, isolation="none")
    no_stage = manager.create_task(project_id=project_id, title="No stage")
    manager.update_task(no_stage.id, allow_automation=True)
    no_match = manager.create_task(project_id=project_id, title="No match", category="code")
    manager.initialize_task_manifest(no_match.id, stage_names=["merge"])
    manager.update_task(no_match.id, allow_automation=True, isolation="none")
    temp_db.execute(
        "UPDATE task_stage_states SET state = 'needs_review' WHERE task_id = ?",
        (no_match.id,),
    )
    ready = _automated_task(temp_db, project_id, "Ready")

    cases = {
        disabled.id: "automation_disabled",
        claimed.id: "claimed",
        mutexed.id: "active_mutex",
        blocked.id: "dependency_block",
        ancestor_blocked.id: "ancestor_stage_pending",
        no_stage.id: "no_current_stage",
        no_match.id: "no_matching_rule",
    }
    for task_id, reason in cases.items():
        assert explain_dispatch(task_id, db=temp_db, project_id=project_id)["reason"] == reason

    explanation = explain_dispatch(ready.id, db=temp_db, project_id=project_id)
    assert explanation["eligible"] is True
    assert explanation["proposed_action"]["action"] == "start_stage"

    blocked_explanation = explain_dispatch(ancestor_blocked.id, db=temp_db, project_id=project_id)
    assert blocked_explanation["ancestor_gate"]["ancestor_ref"] == f"#{parent.seq_num}"
    assert blocked_explanation["ancestor_gate"]["stage_name"] == "expansion"
    assert blocked_explanation["ancestor_gate"]["stage_state"] == "needs_review"

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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
        VALUES (%s, %s, 'dispatcher', 'system', '## Holistic QA Failure\n\nNeeds work')
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


def test_get_build_status_reports_active_run_expired_mutex_lease(temp_db) -> None:
    from gobby.build.observability import explain_dispatch, get_build_status
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import SYSTEM_SESSION_ID
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

    project_id = _project(temp_db, "observability-active-expired")
    task = _automated_task(temp_db, project_id)
    run = LocalAgentRunManager(temp_db).create(
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
        run_id=run.id,
        now=datetime.now(UTC) - timedelta(minutes=5),
    )

    status = get_build_status(f"#{task.seq_num}", db=temp_db, project_id=project_id)

    mutex = status["mutexes"][0]
    assert mutex["state"] == "active_run_expired_lease"
    assert mutex["blocks_dispatch"] is True
    assert mutex["lease_expired"] is True
    assert mutex["run_active"] is True

    explanation = explain_dispatch(task.id, db=temp_db, project_id=project_id)
    assert explanation["reason"] == "active_mutex"
    assert explanation["mutex"]["state"] == "active_run_expired_lease"


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


def test_build_stop_target_disables_status_and_dispatch_for_tree(temp_db) -> None:
    from gobby.build.controls import build_stop_target
    from gobby.build.observability import explain_dispatch, get_build_status
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.tasks._lifecycle_events import BUILD_EVENT_REASON

    project_id = _project(temp_db, "observability-stop-target")
    manager = LocalTaskManager(temp_db)
    root = manager.create_task(project_id=project_id, title="Root", task_type="epic")
    manager.initialize_task_manifest(root.id, stage_names=["planning", "development"])
    root = manager.update_task(root.id, allow_automation=True, isolation="none")
    child = manager.create_task(project_id=project_id, title="Child", parent_task_id=root.id)
    manager.initialize_task_manifest(child.id, stage_names=["development"])
    child = manager.update_task(child.id, allow_automation=True, isolation="none")
    manager.lifecycle_events.record_lifecycle_event(
        root.id,
        from_state=None,
        to_state="planning",
        reason=BUILD_EVENT_REASON,
        by_actor="build",
    )

    result = asyncio.run(build_stop_target(f"#{root.seq_num}", db=temp_db, project_id=project_id))

    assert result.automation_updated == 2
    status = get_build_status(f"#{root.seq_num}", db=temp_db, project_id=project_id)
    assert status["summary"]["state"] == "paused"
    assert status["summary"]["automation_enabled_tasks"] == 0
    assert status["resume_summary"]["can_resume"] is True
    by_id = {task["task_id"]: task for task in status["tasks"]}
    assert by_id[root.id]["allow_automation"] is False
    assert by_id[child.id]["allow_automation"] is False

    explanation = explain_dispatch(root.id, db=temp_db, project_id=project_id)
    assert explanation["eligible"] is False
    assert explanation["reason"] == "automation_disabled"


def test_build_stop_target_preserves_review_approved_stage(temp_db) -> None:
    from gobby.build.controls import build_stop_target
    from gobby.build.observability import explain_dispatch
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.tasks._lifecycle_events import BUILD_EVENT_REASON

    project_id = _project(temp_db, "observability-stop-approved")
    manager = LocalTaskManager(temp_db)
    root = manager.create_task(project_id=project_id, title="Root", task_type="epic")
    manager.initialize_task_manifest(root.id, stage_names=["expansion", "development"])
    root = manager.update_task(root.id, allow_automation=True, isolation="none")
    manager.lifecycle_events.record_lifecycle_event(
        root.id,
        from_state=None,
        to_state="expansion",
        reason=BUILD_EVENT_REASON,
        by_actor="build",
    )
    manager.stage_states.start_stage(root.id, "expansion", by_session_id="dispatcher")
    manager.stage_states.submit_for_review(root.id, "expansion", by_session_id="system")
    manager.stage_states.approve_review(root.id, "expansion", by_session_id="reviewer")

    result = asyncio.run(build_stop_target(f"#{root.seq_num}", db=temp_db, project_id=project_id))

    assert result.stages_reset == 0
    stage = manager.stage_states.current_stage(root.id)
    assert stage is not None
    assert stage.stage_name == "expansion"
    assert stage.state == "review_approved"

    manager.update_task(root.id, allow_automation=True)
    explanation = explain_dispatch(root.id, db=temp_db, project_id=project_id)
    assert explanation["eligible"] is True
    assert explanation["proposed_action"]["action"] == "advance_stage"
    assert explanation["proposed_action"]["stage_name"] == "expansion"


def test_build_resume_target_reopens_project_gate_before_dispatch(temp_db, monkeypatch) -> None:
    from gobby.build import controls
    from gobby.build.dispatch_tick import DispatcherTickSummary
    from gobby.build.project_state import is_project_automation_enabled
    from gobby.build.service import build_stop
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.tasks._lifecycle_events import BUILD_EVENT_REASON

    project_id = _project(temp_db, "observability-resume-target")
    manager = LocalTaskManager(temp_db)
    root = manager.create_task(project_id=project_id, title="Root", task_type="epic")
    manager.initialize_task_manifest(root.id, stage_names=["planning", "development"])
    root = manager.update_task(root.id, allow_automation=False, isolation="none")
    manager.lifecycle_events.record_lifecycle_event(
        root.id,
        from_state=None,
        to_state="planning",
        reason=BUILD_EVENT_REASON,
        by_actor="build",
    )
    build_stop(db=temp_db, project_id=project_id)

    async def fake_kick_dispatcher_tick(db, project_id, **kwargs):
        assert is_project_automation_enabled(db, project_id) is True
        return DispatcherTickSummary(ticks=1, scanned=1, executed=1)

    monkeypatch.setattr(controls, "_kick_dispatcher_tick", fake_kick_dispatcher_tick)

    result = asyncio.run(
        controls.build_resume_target(f"#{root.seq_num}", db=temp_db, project_id=project_id)
    )

    assert result.automation_updated == 1
    assert result.dispatcher_tick is not None
    assert result.dispatcher_tick.reason is None
    assert result.dispatcher_tick.executed == 1
    assert is_project_automation_enabled(temp_db, project_id) is True
    assert manager.get_task(root.id).allow_automation is True


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


def test_get_build_status_hides_stale_current_stage_for_closed_root(temp_db) -> None:
    from gobby.build.observability import get_build_status
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.tasks._lifecycle_events import BUILD_EVENT_REASON
    from tests.storage.tasks._stage_test_helpers import set_stage_state

    project_id = _project(temp_db, "observability-stale-closed-root")
    manager = LocalTaskManager(temp_db)
    root = _automated_task(temp_db, project_id, "Stale Closed Root")
    manager.lifecycle_events.record_lifecycle_event(
        root.id,
        from_state=None,
        to_state="development",
        reason=BUILD_EVENT_REASON,
        by_actor="build",
    )
    set_stage_state(temp_db, root.id, "development", "in_progress")
    temp_db.execute(
        """
        UPDATE tasks
           SET closed_at = %s,
               closed_reason = %s
         WHERE id = %s
        """,
        ("2026-06-02T00:00:00+00:00", "closed-with-stale-stage", root.id),
    )

    status = get_build_status(f"#{root.seq_num}", db=temp_db, project_id=project_id)
    root_status = status["tasks"][0]

    assert status["summary"]["state"] == "completed"
    assert root_status["closed"] is True
    assert root_status["current_stage"] is None


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
        "INSERT INTO task_dependencies (task_id, depends_on, dep_type, created_at) VALUES (%s, %s, 'blocks', NOW())",
        (blocked.id, blocker.id),
    )
    parent = manager.create_task(project_id=project_id, title="Parent", task_type="epic")
    manager.update_task(parent.id, allow_automation=False, isolation="none")
    manager.initialize_task_manifest(
        parent.id, stage_names=["planning", "expansion", "development"]
    )
    temp_db.execute(
        "UPDATE task_stage_states SET state = 'done' WHERE task_id = %s AND stage_name = 'planning'",
        (parent.id,),
    )
    temp_db.execute(
        "UPDATE task_stage_states SET state = 'needs_review' WHERE task_id = %s AND stage_name = 'expansion'",
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
        "UPDATE task_stage_states SET state = 'needs_review' WHERE task_id = %s",
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


def test_explain_dispatch_reports_holistic_descendant_gate(temp_db) -> None:
    from gobby.build.observability import explain_dispatch
    from gobby.storage.tasks import LocalTaskManager

    project_id = _project(temp_db, "observability-holistic-gate")
    manager = LocalTaskManager(temp_db)
    root = manager.create_task(project_id=project_id, title="Root", task_type="epic")
    manager.update_task(root.id, allow_automation=True, isolation="none")
    manager.initialize_task_manifest(root.id, stage_names=["development", "holistic_qa", "merge"])
    temp_db.execute(
        "UPDATE task_stage_states SET state = 'done' WHERE task_id = %s AND stage_name = 'development'",
        (root.id,),
    )
    temp_db.execute(
        "UPDATE task_stage_states SET state = 'ready' WHERE task_id = %s AND stage_name = 'holistic_qa'",
        (root.id,),
    )
    phase = manager.create_task(
        project_id=project_id, title="Integrated phase", parent_task_id=root.id
    )
    child = manager.create_task(
        project_id=project_id,
        title="Reopened child",
        category="code",
        parent_task_id=phase.id,
    )
    manager.initialize_task_manifest(child.id, stage_names=["development"])

    explanation = explain_dispatch(root.id, db=temp_db, project_id=project_id)

    assert explanation["eligible"] is False
    assert explanation["reason"] == "holistic_descendants_nonterminal"
    gate = explanation["holistic_descendant_gate"]
    assert gate["reason"] == "holistic_descendants_nonterminal"
    assert gate["blockers"][0]["task_id"] == child.id
    assert gate["blockers"][0]["task_ref"] == f"#{child.seq_num}"
    assert gate["blockers"][0]["stage_name"] == "development"
    assert gate["blockers"][0]["stage_state"] == "ready"
    assert explanation["proposed_action"] is None

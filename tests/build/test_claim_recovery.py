"""Build-time recovery for safe automation claims."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskArtifactManager
from tests.storage.tasks._stage_test_helpers import initialize_manifest, set_stage_state, spec

pytestmark = pytest.mark.unit


def _claimed_review_task(
    temp_db,
    sample_project: dict[str, str],
    tmp_path: Path,
    *,
    stage_state: str,
    family: str = "worktree",
) -> Task:
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title=f"Claim recovery {stage_state}",
        category="code",
        task_type="task",
    )
    task_manager.update_task(task.id, allow_automation=True, isolation=family)
    initialize_manifest(temp_db, task.id, [spec("development", 0)])
    set_stage_state(temp_db, task.id, "development", stage_state)

    owner = SessionManager(temp_db).register(
        external_id=f"claim-owner-{stage_state}",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    task_manager.claim_task(task.id, owner.id)

    artifact_path = tmp_path / f"{family}-{stage_state}"
    artifact_path.mkdir()
    artifacts = TaskArtifactManager(temp_db)
    if family == "clone":
        artifacts.set_artifacts_atomic(
            task.id,
            clone_path=str(artifact_path),
            clone_id=f"clone-{task.seq_num}",
            base_commit_sha="abc123",
        )
    else:
        artifacts.set_artifacts_atomic(
            task.id,
            worktree_path=str(artifact_path),
            worktree_id=f"wt-{task.seq_num}",
            base_commit_sha="abc123",
        )
    return task


def _claim_recovery_payloads(temp_db, task_id: str) -> list[dict[str, object]]:
    rows = temp_db.fetchall(
        """
        SELECT payload_json
          FROM build_history_events
         WHERE task_id = %s
           AND event_type = 'build_claim_recovery'
         ORDER BY id
        """,
        (task_id,),
    )
    return [json.loads(row["payload_json"]) for row in rows]


@pytest.mark.asyncio
async def test_kick_releases_claimed_needs_review_clean_workspace(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build import claim_recovery
    from gobby.build.dispatch_tick import kick_dispatcher_tick
    from gobby.dispatch.dispatcher import HeartbeatResult
    from gobby.storage.tasks._crud import list_automation_candidates

    task = _claimed_review_task(
        temp_db,
        sample_project,
        tmp_path,
        stage_state="needs_review",
    )
    monkeypatch.setattr(claim_recovery, "_git_status_lines", lambda _path: ([], None))
    monkeypatch.setattr(
        "gobby.dispatch.dispatcher.run_heartbeat",
        AsyncMock(return_value=HeartbeatResult(scanned=1)),
    )

    await kick_dispatcher_tick(temp_db, sample_project["id"], max_ticks=1)

    assert LocalTaskManager(temp_db).get_task(task.id).claimed_by_session_id is None
    candidates = list_automation_candidates(temp_db, project_id=sample_project["id"])
    assert task.id in {candidate.id for candidate in candidates}
    payloads = _claim_recovery_payloads(temp_db, task.id)
    assert payloads[-1]["outcome"] == "released"


def test_recovery_releases_claimed_review_approved_clean_clone(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build import claim_recovery

    task = _claimed_review_task(
        temp_db,
        sample_project,
        tmp_path,
        stage_state="review_approved",
        family="clone",
    )
    monkeypatch.setattr(claim_recovery, "_git_status_lines", lambda _path: ([], None))

    summary = claim_recovery.recover_safe_build_claims(temp_db, sample_project["id"])

    assert summary.released == 1
    assert LocalTaskManager(temp_db).get_task(task.id).claimed_by_session_id is None
    payloads = _claim_recovery_payloads(temp_db, task.id)
    assert payloads[-1]["outcome"] == "released"


def test_recovery_refuses_claimed_in_progress_task(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build import claim_recovery

    task = _claimed_review_task(
        temp_db,
        sample_project,
        tmp_path,
        stage_state="in_progress",
    )
    original_claim = LocalTaskManager(temp_db).get_task(task.id).claimed_by_session_id
    monkeypatch.setattr(claim_recovery, "_git_status_lines", lambda _path: ([], None))

    summary = claim_recovery.recover_safe_build_claims(temp_db, sample_project["id"])

    assert summary.refused == 1
    assert LocalTaskManager(temp_db).get_task(task.id).claimed_by_session_id == original_claim
    payloads = _claim_recovery_payloads(temp_db, task.id)
    assert payloads[-1]["outcome"] == "refused"
    assert payloads[-1]["reason"] == "unsafe_stage"


def test_recovery_refuses_dirty_review_workspace_and_records_audit(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build import claim_recovery

    dirty_files = [" M modified.py", "A  staged.py", "?? new.py"]
    task = _claimed_review_task(
        temp_db,
        sample_project,
        tmp_path,
        stage_state="needs_review",
    )
    original_claim = LocalTaskManager(temp_db).get_task(task.id).claimed_by_session_id
    monkeypatch.setattr(claim_recovery, "_git_status_lines", lambda _path: (dirty_files, None))

    summary = claim_recovery.recover_safe_build_claims(temp_db, sample_project["id"])

    assert summary.refused == 1
    assert LocalTaskManager(temp_db).get_task(task.id).claimed_by_session_id == original_claim
    payloads = _claim_recovery_payloads(temp_db, task.id)
    assert payloads[-1]["outcome"] == "refused"
    assert payloads[-1]["reason"] == "dirty_workspace"
    assert payloads[-1]["workspace"]["dirty_files"] == dirty_files


def test_recovery_preserves_active_agent_owned_claim(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build import claim_recovery

    task = _claimed_review_task(
        temp_db,
        sample_project,
        tmp_path,
        stage_state="needs_review",
    )
    task_manager = LocalTaskManager(temp_db)
    owner_id = task_manager.get_task(task.id).claimed_by_session_id
    assert owner_id is not None

    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id="claim-recovery-parent",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    run_manager = LocalAgentRunManager(temp_db)
    run = run_manager.create(
        parent_session_id=parent.id,
        child_session_id=owner_id,
        claimed_session_id=owner_id,
        provider="codex",
        prompt="work",
        task_id=task.id,
        run_id="run-active-claim-recovery",
    )
    run_manager.start(run.id)
    monkeypatch.setattr(claim_recovery, "_git_status_lines", lambda _path: ([], None))

    summary = claim_recovery.recover_safe_build_claims(temp_db, sample_project["id"])

    assert summary.refused == 1
    assert task_manager.get_task(task.id).claimed_by_session_id == owner_id
    payloads = _claim_recovery_payloads(temp_db, task.id)
    assert payloads[-1]["reason"] == "active_agent_owned"
    assert payloads[-1]["agent_run_id"] == run.id


def test_recovery_releases_terminal_reviewer_owned_claim(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build import claim_recovery

    task = _claimed_review_task(
        temp_db,
        sample_project,
        tmp_path,
        stage_state="needs_review",
    )
    task_manager = LocalTaskManager(temp_db)
    owner_id = task_manager.get_task(task.id).claimed_by_session_id
    assert owner_id is not None

    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id="claim-recovery-terminal-parent",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    run_manager = LocalAgentRunManager(temp_db)
    run = run_manager.create(
        parent_session_id=parent.id,
        child_session_id=owner_id,
        claimed_session_id=owner_id,
        provider="codex",
        prompt="review",
        task_id=task.id,
        agent_name="qa-reviewer",
        run_id="run-terminal-claim-recovery",
    )
    run_manager.start(run.id)
    run_manager.complete(run.id, result="review done")
    monkeypatch.setattr(claim_recovery, "_git_status_lines", lambda _path: ([], None))

    summary = claim_recovery.recover_safe_build_claims(temp_db, sample_project["id"])

    assert summary.released == 1
    assert task_manager.get_task(task.id).claimed_by_session_id is None
    payloads = _claim_recovery_payloads(temp_db, task.id)
    assert payloads[-1]["outcome"] == "released"
    assert payloads[-1]["reason"] == "review_safe_workspace_clean"

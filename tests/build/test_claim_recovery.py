"""Build-time recovery for safe automation claims."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskArtifactManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
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
        validation_criteria="Test task completion is observable.",
    )
    task_manager.update_task(task.id, allow_automation=True, isolation=family)
    initialize_manifest(temp_db, task.id, [spec("development", 0)])
    set_stage_state(temp_db, task.id, "development", stage_state)

    owner = SessionManager(temp_db).register(
        external_id=f"claim-owner-{stage_state}",
        machine_id="21000000-0000-4000-8000-000000000001",
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
            clone_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"gobby:test:clone-{task.seq_num}")),
            base_commit_sha="abc123",
        )
    else:
        artifacts.set_artifacts_atomic(
            task.id,
            worktree_path=str(artifact_path),
            worktree_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"gobby:test:wt-{task.seq_num}")),
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
    from gobby.storage.tasks._automation import list_automation_candidates

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


def test_recovery_refuses_claim_release_when_dispatch_mutex_active(
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
    )
    TaskDispatchMutexManager(temp_db).acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn",
        ttl_seconds=30,
    )
    monkeypatch.setattr(claim_recovery, "_git_status_lines", lambda _path: ([], None))

    summary = claim_recovery.recover_safe_build_claims(temp_db, sample_project["id"])

    assert summary.refused == 1
    assert LocalTaskManager(temp_db).get_task(task.id).claimed_by_session_id is not None
    payloads = _claim_recovery_payloads(temp_db, task.id)
    assert payloads[-1]["outcome"] == "refused"
    assert payloads[-1]["reason"] == "active_dispatch_mutex"


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


def test_recovery_defers_workspace_inspection_after_cap(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
    tmp_path: Path,
) -> None:
    from gobby.build import claim_recovery

    inspected: list[Path] = []
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = _claimed_review_task(
        temp_db,
        sample_project,
        first_root,
        stage_state="needs_review",
    )
    second = _claimed_review_task(
        temp_db,
        sample_project,
        second_root,
        stage_state="needs_review",
    )

    def clean_status(path: Path) -> tuple[list[str], str | None]:
        inspected.append(path)
        return [], None

    monkeypatch.setattr(claim_recovery, "_git_status_lines", clean_status)

    summary = claim_recovery.recover_safe_build_claims(
        temp_db,
        sample_project["id"],
        max_workspace_inspections=1,
    )

    assert len(inspected) == 1
    assert summary.released == 1
    assert summary.refused == 1
    assert LocalTaskManager(temp_db).get_task(first.id).claimed_by_session_id is None
    assert LocalTaskManager(temp_db).get_task(second.id).claimed_by_session_id is not None
    payloads = _claim_recovery_payloads(temp_db, second.id)
    assert payloads[-1]["outcome"] == "refused"
    assert payloads[-1]["reason"] == "workspace_inspection_deferred"


@pytest.mark.asyncio
async def test_kick_dispatcher_tick_offloads_claim_recovery_with_cap(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.build import dispatch_tick
    from gobby.build.claim_recovery import ClaimRecoverySummary
    from gobby.build.dispatch_tick import kick_dispatcher_tick
    from gobby.dispatch.dispatcher import HeartbeatResult

    to_thread_calls: list[dict[str, object]] = []

    def recover_stub(*_args: object, **_kwargs: object) -> ClaimRecoverySummary:
        return ClaimRecoverySummary()

    async def to_thread_stub(
        func: Callable[..., object],
        /,
        *args: object,
        **kwargs: object,
    ) -> object:
        to_thread_calls.append({"func": func, "args": args, "kwargs": kwargs})
        return func(*args, **kwargs)

    monkeypatch.setattr(dispatch_tick, "recover_safe_build_claims", recover_stub)
    monkeypatch.setattr(dispatch_tick.asyncio, "to_thread", to_thread_stub)
    monkeypatch.setattr(
        "gobby.dispatch.dispatcher.run_heartbeat",
        AsyncMock(return_value=HeartbeatResult(scanned=0, reason="no_ready_tasks")),
    )

    await kick_dispatcher_tick(temp_db, sample_project["id"], max_ticks=1)

    assert to_thread_calls
    assert to_thread_calls[0]["func"] is recover_stub
    assert to_thread_calls[0]["kwargs"] == {
        "project_id": sample_project["id"],
        "max_workspace_inspections": 5,
    }


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
        machine_id="21000000-0000-4000-8000-000000000001",
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
        run_id="95a313d5-3aa5-512b-9730-96926fa48273",
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
        machine_id="21000000-0000-4000-8000-000000000001",
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
        run_id="2ada11b9-5925-5f04-89ae-e8356c4c3679",
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

"""Storage tests for expired live-session claim recovery."""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import SESSION_REVIVAL_HORIZON_HOURS
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._live_session_recovery import recover_expired_live_session_claims
from gobby.storage.tasks._transitions import escalate_task_if_owned, release_task_claim_if_owned
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-00000000000f"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _project_id(temp_db: HubDatabase, repo_path: Path) -> str:
    project = LocalProjectManager(temp_db).create(
        name=f"live-recovery-{uuid.uuid4()}",
        repo_path=str(repo_path),
    )
    return project.id


def _session(
    temp_db: HubDatabase,
    project_id: str,
    repo_path: Path,
    *,
    status: str = "expired",
    tmux_pane: str | None = None,
) -> Session:
    manager = SessionManager(temp_db)
    terminal_context: dict[str, str] = {"cwd": str(repo_path)}
    if tmux_pane is not None:
        terminal_context["tmux_pane"] = tmux_pane
        terminal_context["tmux_socket_path"] = "/tmp/tmux-501/default"
    session = manager.register(
        external_id=f"ext-{uuid.uuid4()}",
        machine_id="21000000-0000-4000-8000-00000000000f",
        source="codex",
        project_id=project_id,
        terminal_context=terminal_context,
    )
    if status != "active":
        manager.update_status(session.id, status)
    refreshed = manager.get(session.id)
    assert refreshed is not None
    return refreshed


def _live_task(temp_db: HubDatabase, project_id: str, owner: str, *, title: str) -> Task:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=project_id,
        title=title,
        category="test",
        labels=["live-session"],
        validation_criteria="Expired claim recovery is observable.",
    )
    return manager.claim_task(task.id, owner)


def _set_claim_variables(
    temp_db: HubDatabase,
    owner: str,
    tasks: list[Task],
    *,
    task_edited_files: dict[str, list[str]] | None = None,
) -> None:
    SessionVariableManager(temp_db).merge_variables(
        owner,
        {
            "task_claimed": True,
            "claimed_tasks": {task.id: f"#{task.seq_num}" for task in tasks},
            "active_task_id": tasks[0].id,
            "task_edited_files": task_edited_files or {},
        },
    )


def test_releases_clean_claim_and_clears_session_variables(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = _project_id(temp_db, tmp_path)
    session = _session(temp_db, project_id, tmp_path)
    task = _live_task(temp_db, project_id, session.id, title="Clean live task")
    _set_claim_variables(temp_db, session.id, [task])

    result = recover_expired_live_session_claims(temp_db, project_id=project_id)

    assert result.released == 1
    assert result.escalated == 0
    assert LocalTaskManager(temp_db).get_task(task.id).claimed_by_session_id is None
    variables = SessionVariableManager(temp_db).get_variables(session.id)
    assert variables["task_claimed"] is False
    assert task.id not in variables["claimed_tasks"]


@pytest.mark.parametrize("status", ["active", "paused", "handoff_ready"])
def test_preserves_claims_for_live_owner_statuses(
    temp_db: HubDatabase,
    tmp_path: Path,
    status: str,
) -> None:
    project_id = _project_id(temp_db, tmp_path)
    session = _session(temp_db, project_id, tmp_path, status=status)
    task = _live_task(temp_db, project_id, session.id, title=f"{status} live task")

    result = recover_expired_live_session_claims(temp_db, project_id=project_id)

    assert result.released == 0
    assert result.escalated == 0
    assert LocalTaskManager(temp_db).get_task(task.id).claimed_by_session_id == session.id


def test_escalates_only_task_with_attributed_dirty_paths(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    subprocess.run(
        ["git", "init"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    (repo_path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    project_id = _project_id(temp_db, repo_path)
    session = _session(temp_db, project_id, repo_path)
    dirty_task = _live_task(temp_db, project_id, session.id, title="Dirty live task")
    clean_task = _live_task(temp_db, project_id, session.id, title="Clean sibling task")
    _set_claim_variables(
        temp_db,
        session.id,
        [dirty_task, clean_task],
        task_edited_files={dirty_task.id: ["dirty.txt"]},
    )

    result = recover_expired_live_session_claims(temp_db, project_id=project_id)

    manager = LocalTaskManager(temp_db)
    recovered_dirty = manager.get_task(dirty_task.id)
    recovered_clean = manager.get_task(clean_task.id)
    assert result.escalated == 1
    assert result.released == 1
    assert recovered_dirty.is_escalated
    assert f"#{session.seq_num}" in (recovered_dirty.escalation_reason or "")
    assert "dirty.txt" in (recovered_dirty.escalation_reason or "")
    assert recovered_clean.claimed_by_session_id is None


def test_escalates_when_attributed_dirty_state_is_indeterminate(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = _project_id(temp_db, tmp_path)
    session = _session(temp_db, project_id, tmp_path)
    task = _live_task(temp_db, project_id, session.id, title="Indeterminate live task")
    _set_claim_variables(
        temp_db,
        session.id,
        [task],
        task_edited_files={task.id: ["missing-workspace.txt"]},
    )

    result = recover_expired_live_session_claims(temp_db, project_id=project_id)

    recovered = LocalTaskManager(temp_db).get_task(task.id)
    assert result.escalated == 1
    assert recovered.is_escalated
    assert "indeterminate dirty state" in (recovered.escalation_reason or "")
    assert "missing-workspace.txt" in (recovered.escalation_reason or "")


def test_escalates_when_session_variable_state_is_missing(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = _project_id(temp_db, tmp_path)
    session = _session(temp_db, project_id, tmp_path)
    task = _live_task(temp_db, project_id, session.id, title="Missing state live task")

    result = recover_expired_live_session_claims(temp_db, project_id=project_id)

    recovered = LocalTaskManager(temp_db).get_task(task.id)
    assert result.escalated == 1
    assert recovered.is_escalated
    assert "indeterminate dirty state" in (recovered.escalation_reason or "")
    assert "Task-attributed paths: (unavailable)" in (recovered.escalation_reason or "")


def test_escalates_when_owner_session_lookup_fails(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = _project_id(temp_db, tmp_path)
    session = _session(temp_db, project_id, tmp_path)
    task = _live_task(temp_db, project_id, session.id, title="Unloaded owner task")
    _set_claim_variables(temp_db, session.id, [task], task_edited_files={task.id: []})

    def fail_get(self: SessionManager, session_id: str) -> Session | None:
        raise RuntimeError(f"failed to load {session_id}")

    monkeypatch.setattr(SessionManager, "get", fail_get)

    result = recover_expired_live_session_claims(temp_db, project_id=project_id)

    recovered = LocalTaskManager(temp_db).get_task(task.id)
    assert result.escalated == 1
    assert recovered.is_escalated
    assert "indeterminate dirty state" in (recovered.escalation_reason or "")


def test_preserves_a_dirty_claim_and_its_attribution_through_a_contestable_expiry(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """A pane-contest expiry is not a dead owner, so nothing here is recovered.

    SessionStart expires every terminal session sharing a reused terminal
    context before anything validates who owns the pane. Escalating on that and
    popping `task_edited_files` is the partial rollback #20789 removed from the
    escalate path, and `revive_expired_terminal_session` reverses neither.
    """
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True, text=True)
    (repo_path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    project_id = _project_id(temp_db, repo_path)
    session = _session(temp_db, project_id, repo_path, tmux_pane="%20")
    task = _live_task(temp_db, project_id, session.id, title="Contested live task")
    _set_claim_variables(
        temp_db,
        session.id,
        [task],
        task_edited_files={task.id: ["dirty.txt"]},
    )

    result = recover_expired_live_session_claims(temp_db, project_id=project_id)

    recovered = LocalTaskManager(temp_db).get_task(task.id)
    assert (result.released, result.escalated, result.raced) == (0, 0, 0)
    assert recovered.claimed_by_session_id == session.id
    assert not recovered.is_escalated
    variables = SessionVariableManager(temp_db).get_variables(session.id)
    assert variables["task_edited_files"] == {task.id: ["dirty.txt"]}


def test_recovers_a_pane_claim_once_the_revival_horizon_passes(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    """Past the horizon the expiry can no longer be reversed, so recovery runs."""
    project_id = _project_id(temp_db, tmp_path)
    session = _session(temp_db, project_id, tmp_path, tmux_pane="%20")
    temp_db.execute(
        "UPDATE sessions SET updated_at = %s WHERE id = %s",
        (
            datetime.now(UTC) - timedelta(hours=SESSION_REVIVAL_HORIZON_HOURS, minutes=1),
            session.id,
        ),
    )
    task = _live_task(temp_db, project_id, session.id, title="Unrevivable live task")
    _set_claim_variables(temp_db, session.id, [task])

    result = recover_expired_live_session_claims(temp_db, project_id=project_id)

    assert result.released == 1
    assert LocalTaskManager(temp_db).get_task(task.id).claimed_by_session_id is None


def test_release_owned_claim_rejects_unknown_rowcount() -> None:
    db = MagicMock()
    connection = db.transaction.return_value.__enter__.return_value
    connection.execute.return_value.rowcount = -1

    result = release_task_claim_if_owned(
        cast(HubDatabase, db),
        "task-id",
        expected_owner="session-id",
    )

    assert result is None


def test_escalate_owned_claim_rejects_unknown_rowcount() -> None:
    db = MagicMock()
    connection = db.transaction.return_value.__enter__.return_value
    connection.execute.return_value.rowcount = -1

    result = escalate_task_if_owned(
        cast(HubDatabase, db),
        "task-id",
        reason="indeterminate",
        expected_owner="session-id",
    )

    assert result is None


def test_expected_owner_release_is_compare_and_set(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = _project_id(temp_db, tmp_path)
    session = _session(temp_db, project_id, tmp_path)
    task = _live_task(temp_db, project_id, session.id, title="Release race task")
    manager = LocalTaskManager(temp_db)
    newer_owner = _session(temp_db, project_id, tmp_path, status="active")
    manager.claim_task(task.id, newer_owner.id, force=True)

    transitioned = release_task_claim_if_owned(
        temp_db,
        task.id,
        expected_owner=session.id,
    )

    assert transitioned is None
    assert manager.get_task(task.id).claimed_by_session_id == newer_owner.id


def test_expected_owner_escalation_is_compare_and_set(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project_id = _project_id(temp_db, tmp_path)
    session = _session(temp_db, project_id, tmp_path)
    task = _live_task(temp_db, project_id, session.id, title="Escalation race task")
    manager = LocalTaskManager(temp_db)
    newer_owner = _session(temp_db, project_id, tmp_path, status="active")
    manager.claim_task(task.id, newer_owner.id, force=True)

    transitioned = escalate_task_if_owned(
        temp_db,
        task.id,
        reason="stale owner",
        expected_owner=session.id,
    )

    recovered = manager.get_task(task.id)
    assert transitioned is None
    assert recovered.claimed_by_session_id == newer_owner.id
    assert not recovered.is_escalated

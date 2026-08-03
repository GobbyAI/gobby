from __future__ import annotations

from typing import Any

import psycopg
import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_activity import reconcile_compact_session_activity
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit

TERMINAL_CONTEXT = {
    "tmux_pane": "%guard-test",
    "tmux_socket_path": "/tmp/tmux-delete-guard-test",
    "parent_pid": 19332,
    "parent_create_time": 1785396785.0,
}


def _register_session(
    manager: SessionManager,
    project_id: str,
    external_id: str,
    *,
    terminal_context: dict[str, Any] | None = None,
) -> str:
    return manager.register_session(
        external_id=external_id,
        machine_id="20000000-0000-4000-8000-00000000000d",
        source="claude",
        project_id=project_id,
        terminal_context=terminal_context,
    )


def _create_claimed_session(
    db: HubDatabase,
    manager: SessionManager,
    project_id: str,
    external_id: str,
) -> tuple[str, str, int]:
    session_id = _register_session(manager, project_id, external_id)
    task = LocalTaskManager(db).create_task(
        project_id=project_id,
        title=f"Claim held by {external_id}",
        claimed_by_session_id=session_id,
        category="code",
        validation_criteria="The claimed session remains the task owner.",
    )
    assert task.seq_num is not None
    return session_id, task.id, task.seq_num


def test_delete_refuses_claim_holder(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    session_id, task_id, task_seq_num = _create_claimed_session(
        temp_db,
        session_manager,
        sample_project["id"],
        "guarded-claim-holder",
    )

    with pytest.raises(ValueError) as exc_info:
        session_manager.delete(session_id)

    message = str(exc_info.value)
    assert session_id in message
    assert f"claimed tasks #{task_seq_num}" in message
    assert "Release or reassign them before deleting the session" in message
    assert session_manager.get(session_id) is not None
    task_row = temp_db.fetchone(
        "SELECT claimed_by_session_id FROM tasks WHERE id = %s",
        (task_id,),
    )
    assert task_row is not None
    assert str(task_row["claimed_by_session_id"]) == session_id


def test_guarded_compact_ghost_delete_still_works(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    canonical_id = _register_session(
        session_manager,
        sample_project["id"],
        "guarded-canonical",
        terminal_context=dict(TERMINAL_CONTEXT),
    )
    ghost_id = _register_session(
        session_manager,
        sample_project["id"],
        "guarded-ghost",
        terminal_context=dict(TERMINAL_CONTEXT),
    )
    temp_db.execute(
        "UPDATE sessions SET status = 'expired', message_count = 1 WHERE id = %s",
        (canonical_id,),
    )

    resolution = reconcile_compact_session_activity(session_manager, canonical_id)

    assert resolution.success
    assert resolution.deleted_ghost_ids == (ghost_id,)
    assert session_manager.get(ghost_id) is None


def test_guarded_empty_session_prune_still_works(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    session_id = _register_session(
        session_manager,
        sample_project["id"],
        "guarded-prune",
    )
    session_manager.update_status(session_id, "expired")
    temp_db.execute(
        "UPDATE sessions SET updated_at = NOW() - INTERVAL '2 hours' WHERE id = %s",
        (session_id,),
    )

    assert session_manager.prune_empty_sessions(min_age_hours=1) == 1
    assert session_manager.get(session_id) is None


def test_restrict_blocks_direct_sql_delete(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    session_id, task_id, _ = _create_claimed_session(
        temp_db,
        session_manager,
        sample_project["id"],
        "direct-delete-claim-holder",
    )

    with pytest.raises(psycopg.errors.RestrictViolation):
        with temp_db.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE id = %s", (session_id,))

    assert session_manager.get(session_id) is not None
    task_row = temp_db.fetchone(
        "SELECT claimed_by_session_id FROM tasks WHERE id = %s",
        (task_id,),
    )
    assert task_row is not None
    assert str(task_row["claimed_by_session_id"]) == session_id

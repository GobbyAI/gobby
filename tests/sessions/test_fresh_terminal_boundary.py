"""A provider reset replaces the session without transferring its authority."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.sessions.compact_continuation import COMPACT_HANDOFF_MARKER_VARIABLE
from gobby.sessions.handoff import consume_pending_handoff, stage_handoff_attempt
from gobby.sessions.handoff_records import build_handoff_payload
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.state_manager import SessionVariableManager


@pytest.mark.unit
@pytest.mark.parametrize("pending_compact", [False, True])
@pytest.mark.parametrize("already_expired", [False, True])
def test_native_reset_releases_claim_without_inheriting_parentage(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    pending_compact: bool,
    already_expired: bool,
) -> None:
    sessions = SessionManager(temp_db)
    context = {"tty": "/dev/test-fresh", "parent_pid": 4242, "parent_create_time": 1234.5}
    old = sessions.register(
        external_id="old-provider-id",
        machine_id=None,
        source="claude",
        project_id=sample_project["id"],
        terminal_context=context,
    )
    fresh = sessions.register(
        external_id="fresh-provider-id",
        machine_id=old.machine_id,
        source="claude",
        project_id=sample_project["id"],
        terminal_context=context,
    )
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        sample_project["id"],
        title="Unfinished work",
        validation_criteria="Work remains available after a provider reset.",
    )
    tasks.claim_task(task.id, session_id=old.id)
    if pending_compact:
        stage_handoff_attempt(
            temp_db,
            old.id,
            attempt_id="e" * 32,
            handoff=build_handoff_payload(current_state="Stale compact", next_steps=["Continue"]),
        )
        SessionVariableManager(temp_db).merge_variables(
            old.id, {COMPACT_HANDOFF_MARKER_VARIABLE: "compact"}
        )

    if already_expired:
        sessions.update_status(old.id, "expired")
    listener = MagicMock()
    sessions.register_status_transition_listener(listener)

    ended = sessions.bind_native_clear_successor(fresh.id)

    listener.assert_called_once()
    assert ended is not None and ended.id == old.id
    assert ended.status == "expired"
    current = sessions.get(fresh.id)
    assert current is not None
    assert current.parent_session_id is None
    assert current.agent_run_id is None
    unclaimed = tasks.get_task(task.id)
    assert unclaimed.claimed_by_session_id is None
    assert unclaimed.closed_at is None
    assert consume_pending_handoff(temp_db, old.id) is None
    assert COMPACT_HANDOFF_MARKER_VARIABLE not in SessionVariableManager(temp_db).get_variables(
        old.id
    )
    with patch(
        "gobby.storage.sessions._terminal_revival.recorded_process_exited", return_value=False
    ):
        assert not sessions.revive_terminal_session_for_owner(old.id)
    retired = sessions.get(old.id)
    surviving = sessions.get(fresh.id)
    assert retired is not None and retired.status == "expired"
    assert surviving is not None and surviving.status == "active"

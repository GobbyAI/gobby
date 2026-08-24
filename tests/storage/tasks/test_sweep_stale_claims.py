from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from gobby.sessions.compact_continuation import mark_compact_self_continuation_pending
from gobby.sessions.compact_markers import (
    COMPACT_SELF_CONTINUE_FRESH_SECONDS,
    COMPACT_SELF_CONTINUE_VARIABLE,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions._constants import SESSION_REVIVAL_HORIZON_HOURS
from gobby.storage.tasks._automation import list_automation_candidates, sweep_stale_claims
from gobby.storage.tasks._manager import LocalTaskManager
from gobby.storage.tasks._models import Isolation, Task
from tests.storage.tasks._stage_test_helpers import (
    create_task,
    initialize_manifest,
    set_stage_state,
    spec,
)

pytestmark = pytest.mark.unit

SESS_DEAD = str(uuid.uuid4())


def _make_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_id: str,
    status: str,
    *,
    session_type: str = "terminal",
    age: timedelta = timedelta(0),
    tmux_pane: str | None = None,
) -> None:
    stamp = (datetime.now(UTC) - age).isoformat()
    terminal_context = (
        json.dumps({"tmux_pane": tmux_pane, "tmux_socket_path": "/tmp/tmux-501/default"})
        if tmux_pane is not None
        else None
    )
    temp_db.execute(
        """
        INSERT INTO sessions (
            id, external_id, machine_id, source, project_id, status,
            session_type, terminal_context, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            session_id,
            f"ext-{session_id[:8]}",
            "21000000-0000-4000-8000-000000000001",
            "claude",
            sample_project["id"],
            status,
            session_type,
            terminal_context,
            stamp,
            stamp,
        ),
    )


def _claimed_task(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    *,
    claimed_by: str,
    stage_state: str = "ready",
    allow_automation: bool = True,
) -> Task:
    task = cast(
        Task,
        create_task(
            temp_db,
            sample_project,
            title=f"Claimed by {claimed_by}",
            category="test",
            task_type="task",
        ),
    )
    initialize_manifest(temp_db, task.id, [spec("planning", 0)])
    set_stage_state(temp_db, task.id, "planning", stage_state)
    temp_db.execute(
        "UPDATE tasks SET allow_automation = %s, isolation = %s, claimed_by_session_id = %s WHERE id = %s",
        (allow_automation, Isolation.none.value, claimed_by, task.id),
    )
    return task


def _claim(temp_db: HubDatabase, task_id: str) -> str | None:
    row = temp_db.fetchone("SELECT claimed_by_session_id FROM tasks WHERE id = %s", (task_id,))
    return row["claimed_by_session_id"] if row else None


@pytest.mark.parametrize(
    ("status", "age"),
    [
        # A deleted terminal session is never revived, so its claim is free at once.
        ("deleted", timedelta(0)),
        # An expired one is revivable until the horizon passes; past it, so is its claim.
        ("expired", timedelta(hours=SESSION_REVIVAL_HORIZON_HOURS, minutes=1)),
    ],
)
def test_sweep_reclaims_task_claimed_by_unrevivable_terminal_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    status: str,
    age: timedelta,
) -> None:
    _make_session(temp_db, sample_project, SESS_DEAD, status, age=age, tmux_pane="%20")
    task = _claimed_task(temp_db, sample_project, claimed_by=SESS_DEAD)

    reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert reclaimed >= 1
    assert _claim(temp_db, task.id) is None
    candidate_ids = {
        t.id for t in list_automation_candidates(temp_db, project_id=sample_project["id"])
    }
    assert task.id in candidate_ids


def test_sweep_reclaims_non_automation_task_claimed_by_inactive_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    _make_session(
        temp_db,
        sample_project,
        SESS_DEAD,
        "expired",
        age=timedelta(hours=SESSION_REVIVAL_HORIZON_HOURS, minutes=1),
    )
    task = _claimed_task(
        temp_db,
        sample_project,
        claimed_by=SESS_DEAD,
        allow_automation=False,
    )

    reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert reclaimed >= 1
    assert _claim(temp_db, task.id) is None
    candidate_ids = {
        t.id for t in list_automation_candidates(temp_db, project_id=sample_project["id"])
    }
    assert task.id not in candidate_ids


@pytest.mark.parametrize("status", ["active", "paused", "handoff_ready"])
def test_sweep_keeps_task_claimed_by_live_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    status: str,
) -> None:
    session_id = str(uuid.uuid4())
    _make_session(temp_db, sample_project, session_id, status)
    task = _claimed_task(temp_db, sample_project, claimed_by=session_id)

    sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert _claim(temp_db, task.id) == session_id
    candidate_ids = {
        t.id for t in list_automation_candidates(temp_db, project_id=sample_project["id"])
    }
    assert task.id not in candidate_ids


def _backdate_compact_marker(temp_db: HubDatabase, session_id: str, seconds: int) -> None:
    stale = datetime.now(UTC) - timedelta(seconds=seconds)
    temp_db.execute(
        """
        UPDATE session_variables
           SET variables = jsonb_set(
               variables,
               %s::text[],
               to_jsonb(%s::text)
           )
         WHERE session_id = %s
        """,
        (
            [COMPACT_SELF_CONTINUE_VARIABLE, "created_at"],
            stale.isoformat(),
            session_id,
        ),
    )


def test_sweep_preserves_claim_while_compact_marker_is_fresh(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_id = str(uuid.uuid4())
    _make_session(temp_db, sample_project, session_id, "handoff_ready")
    task = _claimed_task(temp_db, sample_project, claimed_by=session_id)
    assert mark_compact_self_continuation_pending(temp_db, session_id)
    # Lifecycle status can be transiently stale before SessionStart consumes the
    # marker; a fresh marker means the owner is resuming and keeps its claim.
    temp_db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (session_id,))

    with caplog.at_level(logging.INFO, logger="gobby.storage.tasks._automation"):
        reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert reclaimed == 0
    assert _claim(temp_db, task.id) == session_id
    assert caplog.messages == []


def test_sweep_reclaims_claim_once_compact_marker_is_stale(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_id = str(uuid.uuid4())
    _make_session(temp_db, sample_project, session_id, "handoff_ready")
    task = _claimed_task(temp_db, sample_project, claimed_by=session_id)
    assert mark_compact_self_continuation_pending(temp_db, session_id)
    _backdate_compact_marker(temp_db, session_id, COMPACT_SELF_CONTINUE_FRESH_SECONDS + 60)
    temp_db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (session_id,))

    with caplog.at_level(logging.INFO, logger="gobby.storage.tasks._automation"):
        reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert reclaimed == 1
    assert _claim(temp_db, task.id) is None
    assert caplog.messages == [
        (
            f"Released task claim task_id={task.id} owner_session_id={session_id} "
            "actor=sweep_stale_claims reason=owner session status is expired"
        )
    ]


def test_sweep_skips_closed_and_escalated_tasks(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    _make_session(temp_db, sample_project, SESS_DEAD, "expired")
    closed = _claimed_task(temp_db, sample_project, claimed_by=SESS_DEAD)
    escalated = _claimed_task(temp_db, sample_project, claimed_by=SESS_DEAD)
    now = datetime.now(UTC).isoformat()
    temp_db.execute("UPDATE tasks SET closed_at = %s WHERE id = %s", (now, closed.id))
    temp_db.execute(
        "UPDATE tasks SET escalated_at = %s, is_escalated = TRUE WHERE id = %s",
        (now, escalated.id),
    )

    sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert _claim(temp_db, closed.id) == SESS_DEAD
    assert _claim(temp_db, escalated.id) == SESS_DEAD


def test_generic_sweep_defers_live_session_claims(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    _make_session(temp_db, sample_project, SESS_DEAD, "expired")
    task = _claimed_task(temp_db, sample_project, claimed_by=SESS_DEAD)
    LocalTaskManager(temp_db).add_label(task.id, "live-session")

    reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert reclaimed == 0
    assert _claim(temp_db, task.id) == SESS_DEAD


def test_sweep_keeps_a_claim_held_by_a_freshly_expired_terminal_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """A speculative expiry must not cost a live session its work.

    SessionStart expires every terminal session sharing a reused terminal
    context before anything validates who actually owns the pane; the
    authoritative resolver, revive_expired_terminal_session, runs later and
    routinely reverses it. Releasing the claim in between destroys the claim
    of a session that is still working.
    """
    session_id = str(uuid.uuid4())
    _make_session(temp_db, sample_project, session_id, "expired", tmux_pane="%20")
    task = _claimed_task(temp_db, sample_project, claimed_by=session_id)

    reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert reclaimed == 0
    assert _claim(temp_db, task.id) == session_id


def test_a_revived_terminal_session_still_holds_the_claim_it_started_with(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """The full cascade: claim, speculative expiry, sweep, revival."""
    session_id = str(uuid.uuid4())
    _make_session(temp_db, sample_project, session_id, "active", tmux_pane="%20")
    task = _claimed_task(temp_db, sample_project, claimed_by=session_id)

    # SessionStart expires the pane's previous owner on context reuse.
    temp_db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (session_id,))
    sweep_stale_claims(temp_db, project_id=sample_project["id"])
    # Ownership reconciliation then finds this session was the real owner.
    temp_db.execute("UPDATE sessions SET status = 'active' WHERE id = %s", (session_id,))

    assert _claim(temp_db, task.id) == session_id


def test_sweep_reclaims_a_claim_held_by_an_expired_non_terminal_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """Only terminal sessions are revivable, so only they earn the grace."""
    session_id = str(uuid.uuid4())
    _make_session(
        temp_db, sample_project, session_id, "expired", session_type="web_chat", tmux_pane="%20"
    )
    task = _claimed_task(temp_db, sample_project, claimed_by=session_id)

    reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert reclaimed >= 1
    assert _claim(temp_db, task.id) is None

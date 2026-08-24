from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from gobby.hooks.event_handlers._session_start.terminal_runtime import (
    expire_stale_terminal_sessions_for_context,
)
from gobby.sessions.compact_continuation import mark_compact_self_continuation_pending
from gobby.sessions.compact_markers import (
    COMPACT_SELF_CONTINUE_FRESH_SECONDS,
    COMPACT_SELF_CONTINUE_VARIABLE,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import SESSION_REVIVAL_HORIZON_HOURS
from gobby.storage.tasks._automation import list_automation_candidates, sweep_stale_claims
from gobby.storage.tasks._manager import LocalTaskManager
from gobby.storage.tasks._models import Isolation, Task
from gobby.terminal_ownership import PaneOwnershipDecision, resolve_pane_ownership
from tests.storage.tasks._stage_test_helpers import (
    create_task,
    initialize_manifest,
    set_stage_state,
    spec,
)

pytestmark = pytest.mark.unit

SESS_DEAD = str(uuid.uuid4())
MACHINE_ID = "21000000-0000-4000-8000-000000000001"
_OUTER_PID = 4100
_INNER_PID = 4200
_PROCESS_CREATE_TIME = 100.0


class _NestedProcess:
    """A pid whose ancestry models a CLI invoked from inside another CLI."""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def create_time(self) -> float:
        return _PROCESS_CREATE_TIME

    def parents(self) -> list[object]:
        return [SimpleNamespace(pid=_OUTER_PID)] if self.pid == _INNER_PID else []


@pytest.fixture
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


def _nested_process_resolve(
    sessions: list[object],
    *,
    requested_session_id: str | None = None,
) -> PaneOwnershipDecision:
    """Resolve ownership for real against a faked process tree, not a faked verdict."""
    return resolve_pane_ownership(
        sessions,
        requested_session_id=requested_session_id,
        process_factory=_NestedProcess,
        process_group_factory=lambda pid: pid,
        foreground_group_factory=lambda pid: pid,
    )


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
            MACHINE_ID,
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


def test_a_nested_cli_start_leaves_the_outer_sessions_claim_intact(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    _local_machine_identity: None,
) -> None:
    """The full cascade, driven through the real lifecycle paths.

    A nested CLI registers on the pane its parent already owns, SessionStart
    expires the parent speculatively, the dispatcher sweeps, and ownership
    reconciliation then names the outer process the owner. The claim the outer
    session started with has to survive all four steps.
    """
    manager = SessionManager(temp_db)
    outer_context = {
        "tmux_pane": "%20",
        "tmux_socket_path": "/tmp/tmux-501/default",
        "parent_pid": _OUTER_PID,
        "parent_create_time": _PROCESS_CREATE_TIME,
    }
    outer = manager.register(
        external_id="outer-cli",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
        terminal_context=outer_context,
    )
    task = _claimed_task(temp_db, sample_project, claimed_by=outer.id)

    inner_context = {**outer_context, "parent_pid": _INNER_PID}
    inner = manager.register(
        external_id="inner-cli",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
        terminal_context=inner_context,
    )
    expire_stale_terminal_sessions_for_context(
        SimpleNamespace(_session_manager=manager, logger=logging.getLogger(__name__)),
        session_id=inner.id,
        project_id=sample_project["id"],
        terminal_context=inner_context,
    )
    expired = manager.get(outer.id)
    assert expired is not None
    assert expired.status == "expired"

    reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    monkeypatch.setattr(
        "gobby.storage.sessions._terminal_revival.resolve_pane_ownership",
        _nested_process_resolve,
    )
    revived = manager.revive_expired_terminal_session(outer.id)

    assert reclaimed == 0
    assert revived is not None
    assert revived.status == "active"
    assert _claim(temp_db, task.id) == outer.id


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

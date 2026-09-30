"""Operator claim hold: a parked seat keeps its claims until resume, release, or horizon."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from gobby.sessions.contested_expiry import contested_expiry_stamp
from gobby.sessions.operator_claim_hold import (
    OPERATOR_CLAIM_HOLD_VARIABLE,
    is_operator_claim_held,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.sessions._constants import SESSION_REVIVAL_HORIZON_HOURS
from gobby.storage.sessions._contested_expiry import read_session_variables
from gobby.storage.sessions._operator_claim_hold import (
    clear_operator_claim_hold,
    record_operator_claim_hold,
)
from gobby.storage.tasks._automation import sweep_stale_claims
from tests.storage.tasks.test_sweep_stale_claims import (
    _EXITED_HOST_EPOCH,
    MACHINE_ID,
    _claim,
    _claimed_task,
    _expire_seat_whose_native_pane_exited,
    _make_session,
)

pytestmark = pytest.mark.unit

_OPERATOR = "operator-session"


@pytest.fixture
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


def _hold(temp_db: HubDatabase, session_id: str) -> None:
    record_operator_claim_hold(
        temp_db, session_id, actor_session_id=_OPERATOR, reason="directed CLI update"
    )


def _restamp_hold(temp_db: HubDatabase, session_id: str, stamp: str) -> None:
    temp_db.execute(
        """
        UPDATE session_variables
           SET variables = jsonb_set(variables, %s::text[], to_jsonb(%s::text))
         WHERE session_id = %s
        """,
        ([OPERATOR_CLAIM_HOLD_VARIABLE, "created_at"], stamp, session_id),
    )


def _held(temp_db: HubDatabase, session_id: str) -> bool:
    return OPERATOR_CLAIM_HOLD_VARIABLE in (read_session_variables(temp_db, session_id) or {})


@pytest.mark.usefixtures("_local_machine_identity")
def test_a_held_seat_keeps_its_claim_through_the_expiry_its_exit_brings(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """The #23080 sequence: paused, pane exits under the live host, expired, swept.

    Without a hold that expiry is final and the claim releases; with one placed
    before the exit, the expiry's status write leaves the hold and the claim.
    """
    _sessions, session_id = _expire_seat_whose_native_pane_exited(
        temp_db,
        sample_project,
        live_host_epoch=_EXITED_HOST_EPOCH,
        before_expiry=lambda held_id: _hold(temp_db, held_id),
    )
    task = _claimed_task(temp_db, sample_project, claimed_by=session_id)

    reclaimed = sweep_stale_claims(temp_db, project_id=sample_project["id"])

    assert (reclaimed, _claim(temp_db, task.id), _held(temp_db, session_id)) == (
        0,
        session_id,
        True,
    )


@pytest.mark.usefixtures("_local_machine_identity")
def test_revival_of_the_held_seat_clears_the_hold_and_keeps_the_claim(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    sessions, session_id = _expire_seat_whose_native_pane_exited(
        temp_db,
        sample_project,
        live_host_epoch="host-epoch-after-update",
        before_expiry=lambda held_id: _hold(temp_db, held_id),
    )
    task = _claimed_task(temp_db, sample_project, claimed_by=session_id)

    revived = sessions.revive_expired_terminal_session(session_id)

    assert revived is not None
    assert revived.status == "active"
    assert _held(temp_db, session_id) is False
    assert sweep_stale_claims(temp_db, project_id=sample_project["id"]) == 0
    assert _claim(temp_db, task.id) == session_id


def test_release_returns_the_claim_to_the_ordinary_schedule(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    session_id = str(uuid.uuid4())
    _make_session(temp_db, sample_project, session_id, "expired")
    _hold(temp_db, session_id)
    task = _claimed_task(temp_db, sample_project, claimed_by=session_id)
    assert sweep_stale_claims(temp_db, project_id=sample_project["id"]) == 0

    released = clear_operator_claim_hold(temp_db, session_id)

    assert released is True
    assert clear_operator_claim_hold(temp_db, session_id) is False
    assert sweep_stale_claims(temp_db, project_id=sample_project["id"]) == 1
    assert _claim(temp_db, task.id) is None


@pytest.mark.parametrize(
    ("status_after_hold",),
    [pytest.param("paused", id="paused"), pytest.param("awaiting_handoff", id="handoff")],
)
def test_status_writes_leave_the_hold_in_place(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    status_after_hold: str,
) -> None:
    session_id = str(uuid.uuid4())
    _make_session(temp_db, sample_project, session_id, "active")
    _hold(temp_db, session_id)

    SessionManager(temp_db).update_status(session_id, status_after_hold)
    SessionManager(temp_db).update_status(session_id, "expired")

    assert _held(temp_db, session_id) is True


_NOW_OFFSET = timedelta(minutes=5)


@pytest.mark.parametrize(
    ("stamp", "expect_held"),
    [
        pytest.param(None, True, id="fresh"),
        pytest.param(
            lambda: contested_expiry_stamp(
                datetime.now(UTC) - timedelta(hours=SESSION_REVIVAL_HORIZON_HOURS) - _NOW_OFFSET
            ),
            False,
            id="past_horizon",
        ),
        pytest.param(
            lambda: contested_expiry_stamp(datetime.now(UTC) + _NOW_OFFSET),
            False,
            id="future",
        ),
        pytest.param(lambda: "2026-09-29T12:00:00Z", False, id="malformed"),
    ],
)
def test_the_python_and_sql_shields_read_the_hold_alike(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    stamp: Any,
    expect_held: bool,
) -> None:
    session_id = str(uuid.uuid4())
    _make_session(temp_db, sample_project, session_id, "expired")
    _hold(temp_db, session_id)
    if stamp is not None:
        _restamp_hold(temp_db, session_id, stamp())
    task = _claimed_task(temp_db, sample_project, claimed_by=session_id)

    python_shields = is_operator_claim_held(read_session_variables(temp_db, session_id))
    sweep_stale_claims(temp_db, project_id=sample_project["id"])
    sql_shields = _claim(temp_db, task.id) == session_id

    assert (python_shields, sql_shields) == (expect_held, expect_held)

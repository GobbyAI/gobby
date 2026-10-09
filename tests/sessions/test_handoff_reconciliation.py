"""An unconfirmed compact attempt settles on its own once its reconcile window closes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.sessions.compact_markers import (
    COMPACT_NOTIFICATION_STARTED_AT_VARIABLE,
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
)
from gobby.sessions.handoff import (
    FAILED_HANDOFF_VARIABLE,
    HANDOFF_DISPATCH_GATE_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    restore_staged_handoff,
    stage_handoff_attempt,
)
from gobby.sessions.handoff_reconciliation import (
    UNCONFIRMED_COMPACT_WINDOW,
    settle_expired_unconfirmed_compact,
)
from gobby.sessions.handoff_records import build_handoff_payload, insert_delivery_receipt
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import write_project_marker

pytestmark = pytest.mark.unit

MACHINE_ID = "20000000-0000-4000-8000-000000000003"
ATTEMPT_ID = "e" * 32
LONG_PAST = UNCONFIRMED_COMPACT_WINDOW + timedelta(hours=3)


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


@pytest.fixture
def session_manager(temp_db: HubDatabase, tmp_path: Path) -> SessionManager:
    checkout = tmp_path / "expiry-test"
    checkout.mkdir()
    project_id = str(uuid4())
    write_project_marker(checkout, project_id=project_id, name="expiry-test")
    project = LocalProjectManager(temp_db).create(
        name="expiry-test", repo_path=str(checkout), project_id=project_id
    )
    manager = SessionManager(temp_db)
    manager.register_session(
        external_id="expiry-session",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=project.id,
    )
    return manager


@pytest.fixture
def session_id(session_manager: SessionManager) -> str:
    row = session_manager.db.fetchone(
        "SELECT id FROM sessions WHERE external_id = %s", ("expiry-session",)
    )
    assert row is not None
    return str(row["id"])


def unconfirmed_attempt(db: HubDatabase, session_id: str, *, age: timedelta) -> datetime:
    """Fail one compact attempt as compact_unconfirmed, authored ``age`` ago."""
    staged = stage_handoff_attempt(
        db,
        session_id,
        attempt_id=ATTEMPT_ID,
        handoff=build_handoff_payload(
            current_state="The compact boundary was never confirmed.",
            next_steps=["Resume the assigned task."],
        ),
        clear_session=False,
    )
    assert restore_staged_handoff(
        db,
        session_id,
        ATTEMPT_ID,
        failure_result={
            "attempt_id": ATTEMPT_ID,
            "clear_session": False,
            "delivery_failed": True,
            "delivery_pending": False,
            "delivery_state": "failed_not_deliverable",
            "error_code": "compact_unconfirmed",
        },
    )
    # deliver_staged_compact_handoff keeps the continuation marker on this path.
    SessionVariableManager(db).set_variable(
        session_id,
        HANDOFF_COMPACT_CONTINUE_VARIABLE,
        {"attempt_id": ATTEMPT_ID, "prompt": "Call get_handoff"},
    )
    with db.transaction() as conn:
        row = conn.execute(
            "UPDATE session_handoffs SET authored_at = now() - %s WHERE id = %s "
            "RETURNING authored_at",
            (age, staged.handoff_record_id),
        ).fetchone()
    assert row is not None
    authored: datetime = row["authored_at"]
    return authored


def receipt_count(db: HubDatabase) -> int:
    row = db.fetchone(
        "SELECT count(*) AS n FROM session_handoff_deliveries WHERE attempt_id = %s",
        (ATTEMPT_ID,),
    )
    assert row is not None
    return int(row["n"])


def write_rollout(db: HubDatabase, session_id: str, tmp_path: Path, *stamps: datetime) -> None:
    """Point the session at a Codex rollout holding compacted records at ``stamps``."""
    external = db.fetchone("SELECT external_id FROM sessions WHERE id = %s", (session_id,))
    assert external is not None
    rollout = tmp_path / "codex-rollout.jsonl"
    lines = [json.dumps({"type": "session_meta", "payload": {"id": external["external_id"]}})]
    lines += [json.dumps({"type": "compacted", "timestamp": stamp.isoformat()}) for stamp in stamps]
    rollout.write_text("\n".join(lines) + "\n")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE sessions SET transcript_path = %s WHERE id = %s", (str(rollout), session_id)
        )


def test_open_window_returns_its_end_and_keeps_the_gate(
    temp_db: HubDatabase, session_id: str
) -> None:
    authored = unconfirmed_attempt(temp_db, session_id, age=timedelta(minutes=1))

    assert settle_expired_unconfirmed_compact(temp_db, session_id, ATTEMPT_ID) == (
        authored + UNCONFIRMED_COMPACT_WINDOW
    )
    variables = SessionVariableManager(temp_db).get_variables(session_id)
    assert variables[HANDOFF_DISPATCH_GATE_VARIABLE]["attempt_id"] == ATTEMPT_ID
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE in variables


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "paused"])
async def test_closed_window_without_boundary_releases_the_gate_and_keeps_the_payload(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    session_id: str,
    tmp_path: Path,
    status: str,
) -> None:
    unconfirmed_attempt(temp_db, session_id, age=LONG_PAST)
    write_rollout(temp_db, session_id, tmp_path)
    with temp_db.transaction() as conn:
        conn.execute("UPDATE sessions SET status = %s WHERE id = %s", (status, session_id))

    assert settle_expired_unconfirmed_compact(temp_db, session_id, ATTEMPT_ID) is True
    assert settle_expired_unconfirmed_compact(temp_db, session_id, ATTEMPT_ID) is False

    variables = SessionVariableManager(temp_db).get_variables(session_id)
    assert HANDOFF_DISPATCH_GATE_VARIABLE not in variables
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in variables
    assert variables[FAILED_HANDOFF_VARIABLE]["attempt_id"] == ATTEMPT_ID
    assert variables[FAILED_HANDOFF_VARIABLE]["delivery_state"] == "failed_not_deliverable"
    registry = create_session_messages_registry(session_manager=session_manager, db=temp_db)
    with session_context_for_test(session_id):
        recovered = await registry.call("get_handoff", {"failed_attempt_id": ATTEMPT_ID})
    assert recovered["found"] is True
    assert recovered["delivery_state"] == "failed_not_deliverable"
    assert receipt_count(temp_db) == 0


def test_expired_session_is_left_alone(temp_db: HubDatabase, session_id: str) -> None:
    unconfirmed_attempt(temp_db, session_id, age=LONG_PAST)
    with temp_db.transaction() as conn:
        conn.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (session_id,))

    assert settle_expired_unconfirmed_compact(temp_db, session_id, ATTEMPT_ID) is False
    variables = SessionVariableManager(temp_db).get_variables(session_id)
    assert variables[HANDOFF_DISPATCH_GATE_VARIABLE]["attempt_id"] == ATTEMPT_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["rollout", "notification"])
async def test_boundary_inside_the_window_as_settle_fires_stays_on_the_reconcile_path(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    session_id: str,
    tmp_path: Path,
    evidence: str,
) -> None:
    authored = unconfirmed_attempt(
        temp_db, session_id, age=UNCONFIRMED_COMPACT_WINDOW + timedelta(seconds=5)
    )
    boundary = authored + UNCONFIRMED_COMPACT_WINDOW - timedelta(seconds=1)
    if evidence == "rollout":
        write_rollout(temp_db, session_id, tmp_path, boundary)
    else:
        SessionVariableManager(temp_db).set_variable(
            session_id, COMPACT_NOTIFICATION_STARTED_AT_VARIABLE, boundary.isoformat()
        )

    assert settle_expired_unconfirmed_compact(temp_db, session_id, ATTEMPT_ID) is False
    assert HANDOFF_DISPATCH_GATE_VARIABLE in SessionVariableManager(temp_db).get_variables(
        session_id
    )
    registry = create_session_messages_registry(session_manager=session_manager, db=temp_db)
    with session_context_for_test(session_id):
        reconciled = await registry.call(
            "get_handoff", {"failed_attempt_id": ATTEMPT_ID, "reconcile_late_compact": True}
        )
    assert reconciled["delivery_state"] == "reconciled_late_compact"
    assert settle_expired_unconfirmed_compact(temp_db, session_id, ATTEMPT_ID) is False
    assert receipt_count(temp_db) == 1


@pytest.mark.asyncio
async def test_boundary_after_settlement_cannot_deliver_the_settled_attempt(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    session_id: str,
    tmp_path: Path,
) -> None:
    authored = unconfirmed_attempt(
        temp_db, session_id, age=UNCONFIRMED_COMPACT_WINDOW + timedelta(seconds=5)
    )
    write_rollout(temp_db, session_id, tmp_path)
    assert settle_expired_unconfirmed_compact(temp_db, session_id, ATTEMPT_ID) is True

    write_rollout(
        temp_db, session_id, tmp_path, authored + UNCONFIRMED_COMPACT_WINDOW + timedelta(seconds=2)
    )
    registry = create_session_messages_registry(session_manager=session_manager, db=temp_db)
    with session_context_for_test(session_id):
        late = await registry.call(
            "get_handoff", {"failed_attempt_id": ATTEMPT_ID, "reconcile_late_compact": True}
        )
        pulled = await registry.call("get_handoff", {})
    assert late["found"] is False
    assert pulled["found"] is False
    assert receipt_count(temp_db) == 0


def test_newer_in_flight_attempt_keeps_its_gate(temp_db: HubDatabase, session_id: str) -> None:
    unconfirmed_attempt(temp_db, session_id, age=LONG_PAST)
    SessionVariableManager(temp_db).set_variable(
        session_id,
        PENDING_HANDOFF_VARIABLE,
        {"attempt_id": "f" * 32, "clear_session": False, "dispatch_started_at": "now"},
    )

    assert settle_expired_unconfirmed_compact(temp_db, session_id, ATTEMPT_ID) is False
    variables = SessionVariableManager(temp_db).get_variables(session_id)
    assert variables[HANDOFF_DISPATCH_GATE_VARIABLE]["attempt_id"] == ATTEMPT_ID


def test_delivered_attempt_is_never_settled(temp_db: HubDatabase, session_id: str) -> None:
    unconfirmed_attempt(temp_db, session_id, age=LONG_PAST)
    marker = SessionVariableManager(temp_db).get_variables(session_id)[FAILED_HANDOFF_VARIABLE]
    with temp_db.transaction() as conn:
        insert_delivery_receipt(
            conn,
            handoff_id=marker["handoff_record_id"],
            attempt_id=ATTEMPT_ID,
            boundary_kind="compact",
            continuation_session_id=session_id,
        )

    assert settle_expired_unconfirmed_compact(temp_db, session_id, ATTEMPT_ID) is False
    variables = SessionVariableManager(temp_db).get_variables(session_id)
    assert variables[HANDOFF_DISPATCH_GATE_VARIABLE]["attempt_id"] == ATTEMPT_ID
    assert receipt_count(temp_db) == 1

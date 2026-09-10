"""Real database proofs for the restart/handoff admission boundary."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from gobby.sessions.handoff import (
    consume_pending_handoff,
    restore_handoff_attempt,
    stage_handoff_attempt,
)
from gobby.sessions.handoff_records import build_handoff_payload, record_handoff_delivery
from gobby.sessions.handoff_shutdown import (
    HandoffShutdownBlocked,
    cancel_handoff_shutdown,
    guard_handoff_shutdown,
    lock_handoff_staging,
    prepare_handoff_shutdown,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager


@pytest.fixture
def machine_id(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    value = "20000000-0000-4000-8000-000000000002"
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", value)
    yield value
    cancel_handoff_shutdown(value)


@pytest.fixture
def handoff_session(temp_db: HubDatabase, machine_id: str) -> str:
    project = LocalProjectManager(temp_db).create(name="handoff-shutdown")
    return SessionManager(temp_db).register_session(
        external_id=str(uuid4()), machine_id=machine_id, source="codex", project_id=project.id
    )


@pytest.mark.parametrize("clear_session", [False, True])
def test_pending_handoff_blocks_until_ack_or_failed_delivery(
    temp_db: HubDatabase, machine_id: str, handoff_session: str, clear_session: bool
) -> None:
    handoff = build_handoff_payload(current_state="Ready", next_steps=["Continue"])
    state = stage_handoff_attempt(
        temp_db,
        handoff_session,
        attempt_id=uuid4().hex,
        handoff=handoff,
        clear_session=clear_session,
    )
    with pytest.raises(HandoffShutdownBlocked, match=state.attempt_id):
        with guard_handoff_shutdown(temp_db, machine_id):
            pytest.fail("Shutdown must not begin")
    if clear_session:
        assert restore_handoff_attempt(temp_db, state)
    else:
        consumed = consume_pending_handoff(temp_db, handoff_session)
        assert consumed is not None and consumed.markdown == handoff.rendered_markdown
    with guard_handoff_shutdown(temp_db, machine_id):
        assert SessionManager(temp_db).get(handoff_session) is not None


def test_expired_clear_predecessor_still_protects_live_successor(
    temp_db: HubDatabase, machine_id: str, handoff_session: str
) -> None:
    manager = SessionManager(temp_db)
    predecessor = manager.get(handoff_session)
    assert predecessor is not None
    state = stage_handoff_attempt(
        temp_db,
        handoff_session,
        attempt_id=uuid4().hex,
        handoff=build_handoff_payload(current_state="Ready", next_steps=["Continue"]),
        clear_session=True,
    )
    successor = manager.register_session(
        external_id=str(uuid4()),
        machine_id=machine_id,
        source="codex",
        project_id=predecessor.project_id,
        parent_session_id=predecessor.id,
    )
    manager.update(handoff_session, status="expired")
    SessionVariableManager(temp_db).merge_variables(
        handoff_session, {"clear_attempt": {"consumed_by": successor}}
    )
    record_handoff_delivery(
        temp_db,
        handoff_id=state.handoff_record_id,
        attempt_id=state.attempt_id,
        boundary_kind="clear",
        continuation_session_id=successor,
    )
    with pytest.raises(HandoffShutdownBlocked, match=state.attempt_id):
        with guard_handoff_shutdown(temp_db, machine_id):
            pytest.fail("The successor has not read its handoff")
    assert consume_pending_handoff(temp_db, successor) is not None
    with guard_handoff_shutdown(temp_db, machine_id):
        assert manager.get(successor) is not None


def test_abandoned_or_other_machine_handoff_does_not_block(
    temp_db: HubDatabase, machine_id: str, handoff_session: str
) -> None:
    state = stage_handoff_attempt(
        temp_db,
        handoff_session,
        attempt_id=uuid4().hex,
        handoff=build_handoff_payload(current_state="Ready", next_steps=["Continue"]),
        clear_session=False,
    )
    with guard_handoff_shutdown(temp_db, str(uuid4())):
        assert state.attempt_id
    SessionManager(temp_db).update(handoff_session, status="expired")
    with guard_handoff_shutdown(temp_db, machine_id):
        assert SessionVariableManager(temp_db).get_variables(handoff_session)["set_handoff_pending"]


def test_cli_shutdown_fence_rejects_new_stage_without_writes(
    temp_db: HubDatabase, machine_id: str, handoff_session: str
) -> None:
    def stage() -> None:
        stage_handoff_attempt(
            temp_db,
            handoff_session,
            attempt_id=uuid4().hex,
            handoff=build_handoff_payload(current_state="Ready", next_steps=["Continue"]),
            clear_session=False,
        )

    with guard_handoff_shutdown(temp_db, machine_id), ThreadPoolExecutor() as executor:
        with pytest.raises(HandoffShutdownBlocked, match="Nothing was staged"):
            executor.submit(stage).result(timeout=5)
    assert "set_handoff_pending" not in SessionVariableManager(temp_db).get_variables(
        handoff_session
    )
    stage()
    assert consume_pending_handoff(temp_db, handoff_session) is not None


def test_inflight_staging_prevents_shutdown_check(temp_db: HubDatabase, machine_id: str) -> None:
    def prepare() -> None:
        prepare_handoff_shutdown(temp_db, machine_id)

    with temp_db.transaction() as conn, ThreadPoolExecutor() as executor:
        lock_handoff_staging(conn, machine_id)
        with pytest.raises(HandoffShutdownBlocked, match="staging"):
            executor.submit(prepare).result(timeout=5)


def test_http_admission_stays_closed_until_shutdown_cancelled(
    temp_db: HubDatabase, machine_id: str, handoff_session: str
) -> None:
    prepare_handoff_shutdown(temp_db, machine_id)
    with pytest.raises(HandoffShutdownBlocked, match="Nothing was staged"):
        stage_handoff_attempt(
            temp_db,
            handoff_session,
            attempt_id=uuid4().hex,
            handoff=build_handoff_payload(current_state="Ready", next_steps=["Continue"]),
            clear_session=False,
        )
    cancel_handoff_shutdown(machine_id)
    with temp_db.transaction() as conn:
        lock_handoff_staging(conn, machine_id)

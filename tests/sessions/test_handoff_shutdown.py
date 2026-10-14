"""Real database proofs for the restart/handoff admission boundary."""

import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from gobby.cli._daemon_handoffs import protect_pending_handoffs
from gobby.cli.runtime import CliRuntime
from gobby.sessions.handoff import (
    consume_pending_handoff,
    restore_handoff_attempt,
    stage_handoff_attempt,
)
from gobby.sessions.handoff_records import build_handoff_payload, record_handoff_delivery
from gobby.sessions.handoff_shutdown import (
    HANDOFF_IN_FLIGHT_MINUTES,
    HandoffShutdownBlocked,
    cancel_handoff_shutdown,
    guard_handoff_shutdown,
    lock_handoff_staging,
    prepare_handoff_shutdown,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.utils.datetime import utc_now
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


@pytest.mark.parametrize("move_marker", [False, True], ids=["unchanged", "marker-moved"])
def test_cli_wait_logs_each_stale_marker_once_per_wait(
    temp_db: HubDatabase,
    machine_id: str,
    handoff_session: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    move_marker: bool,
) -> None:
    sessions = SessionManager(temp_db)
    original = sessions.get(handoff_session)
    assert original is not None
    second_stale, active = (
        sessions.register_session(
            external_id=str(uuid4()),
            machine_id=machine_id,
            source="codex",
            project_id=original.project_id,
        )
        for _ in range(2)
    )
    handoff = build_handoff_payload(current_state="Ready", next_steps=["Continue"])
    variables = SessionVariableManager(temp_db)
    stale_attempts: list[str] = []
    old_time = utc_now() - timedelta(minutes=HANDOFF_IN_FLIGHT_MINUTES + 5)
    for session in (handoff_session, second_stale):
        state = stage_handoff_attempt(
            temp_db, session, attempt_id=uuid4().hex, handoff=handoff, clear_session=False
        )
        stale_attempts.append(state.attempt_id)
        marker = dict(variables.get_variables(session)["set_handoff_pending"])
        marker["created_at"] = old_time.isoformat()
        variables.merge_variables(session, {"set_handoff_pending": marker})

    runtime = MagicMock(spec=CliRuntime)
    runtime.require_database.return_value = temp_db
    messages: list[str] = []
    caplog.set_level(logging.WARNING, logger="gobby.sessions.handoff_shutdown")
    for wait_index in range(2):
        logged_stale_markers: set[tuple[str, str | None, str]] = set()
        stage_handoff_attempt(
            temp_db, active, attempt_id=uuid4().hex, handoff=handoff, clear_session=False
        )
        sleeps = 0

        def acknowledge_after_retries(_seconds: float, *, current_wait: int = wait_index) -> None:
            nonlocal sleeps
            sleeps += 1
            if move_marker and current_wait == 0 and sleeps == 1:
                marker = dict(variables.get_variables(handoff_session)["set_handoff_pending"])
                marker["created_at"] = (old_time + timedelta(minutes=1)).isoformat()
                variables.merge_variables(handoff_session, {"set_handoff_pending": marker})
            if sleeps == 3:
                assert consume_pending_handoff(temp_db, active) is not None

        monkeypatch.setattr("gobby.cli._daemon_handoffs.time.sleep", acknowledge_after_retries)
        with protect_pending_handoffs(
            runtime, wait=True, report=messages.append, logged_stale_markers=logged_stale_markers
        ):
            assert sleeps == 3
        # Cutover releases preflight's fence and reacquires it for the actual stop.
        with protect_pending_handoffs(
            runtime, wait=True, report=messages.append, logged_stale_markers=logged_stale_markers
        ):
            assert variables.get_variables(handoff_session)["set_handoff_pending"]

    warnings = [record.getMessage() for record in caplog.records]
    assert sum(stale_attempts[0] in message for message in warnings) == (3 if move_marker else 2)
    assert sum(stale_attempts[1] in message for message in warnings) == 2
    assert len(messages) == 2
    for session in (handoff_session, second_stale):
        consumed = consume_pending_handoff(temp_db, session)
        assert consumed is not None and consumed.markdown == handoff.rendered_markdown


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


@pytest.mark.parametrize(
    ("claimed", "minutes_ago", "blocks"),
    [
        pytest.param(True, HANDOFF_IN_FLIGHT_MINUTES - 1, True, id="recent-dispatch-blocks"),
        pytest.param(True, HANDOFF_IN_FLIGHT_MINUTES + 1, False, id="stale-dispatch-skipped"),
        pytest.param(False, HANDOFF_IN_FLIGHT_MINUTES + 1, False, id="stale-unclaimed-skipped"),
    ],
)
def test_marker_blocks_only_while_its_delivery_can_still_be_in_flight(
    temp_db: HubDatabase,
    machine_id: str,
    handoff_session: str,
    caplog: pytest.LogCaptureFixture,
    claimed: bool,
    minutes_ago: int,
    blocks: bool,
) -> None:
    """A compact pull left unsubmitted for hours must not fence every restart.

    The compacted row returns to a live status, so the awaiting_handoff sweep
    never expires it; only the marker's own age says the delivery is over.
    """
    handoff = build_handoff_payload(current_state="Ready", next_steps=["Continue"])
    state = stage_handoff_attempt(
        temp_db,
        handoff_session,
        attempt_id=uuid4().hex,
        handoff=handoff,
        clear_session=False,
    )
    variables = SessionVariableManager(temp_db)
    marker = dict(variables.get_variables(handoff_session)["set_handoff_pending"])
    # The delivery hook stamps dispatch_started_at when it claims the attempt; an
    # attempt it never claimed only carries its staging time.
    marker["dispatch_started_at" if claimed else "created_at"] = (
        utc_now() - timedelta(minutes=minutes_ago)
    ).isoformat()
    variables.merge_variables(handoff_session, {"set_handoff_pending": marker})

    with caplog.at_level(logging.WARNING, logger="gobby.sessions.handoff_shutdown"):
        if blocks:
            with pytest.raises(HandoffShutdownBlocked, match=state.attempt_id):
                with guard_handoff_shutdown(temp_db, machine_id):
                    pytest.fail("A delivery this recent may still be in flight")
            assert state.attempt_id not in caplog.text
        else:
            with guard_handoff_shutdown(temp_db, machine_id):
                assert SessionManager(temp_db).get(handoff_session) is not None
            assert "handoff-shutdown#" in caplog.text
            assert state.attempt_id in caplog.text
    # Skipping never discards the handoff: the marker is still consumable afterwards.
    consumed = consume_pending_handoff(temp_db, handoff_session)
    assert consumed is not None and consumed.markdown == handoff.rendered_markdown


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


def test_shutdown_guard_refuses_a_second_connection(temp_db: HubDatabase, machine_id: str) -> None:
    def second_shutdown() -> None:
        with guard_handoff_shutdown(temp_db, machine_id):
            pytest.fail("A second connection acquired the exclusive shutdown guard")

    with guard_handoff_shutdown(temp_db, machine_id), ThreadPoolExecutor() as executor:
        with pytest.raises(HandoffShutdownBlocked, match="another shutdown is in progress"):
            executor.submit(second_shutdown).result(timeout=5)
    with guard_handoff_shutdown(temp_db, machine_id):
        pass


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

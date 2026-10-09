"""The daemon releases an unconfirmed compact's retry gate once its reconcile window closes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks import terminal_handoff_delivery
from gobby.hooks.terminal_handoff_delivery import (
    _compensate_delivery_failure,
    _release_expired_compact,
    resume_dead_handoff_dispatches,
    resume_unconfirmed_compact_expiries,
)
from gobby.sessions.compact_continuation import _HANDOFF_COMPACT_CONTINUATION_TASKS
from gobby.sessions.compact_markers import (
    COMPACT_NOTIFICATION_STARTED_AT_VARIABLE,
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
)
from gobby.sessions.handoff import (
    FAILED_HANDOFF_VARIABLE,
    HANDOFF_DISPATCH_GATE_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    ClaimedHandoffDelivery,
    claim_staged_handoff_delivery,
    stage_handoff_attempt,
)
from gobby.sessions.handoff_reconciliation import UNCONFIRMED_COMPACT_WINDOW
from gobby.sessions.handoff_records import build_handoff_payload, record_handoff_delivery
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000001"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"
STRANDED = "41000000-0000-4000-8000-000000000001"
EXPIRED = "41000000-0000-4000-8000-000000000002"
SUPERSEDED = "41000000-0000-4000-8000-000000000003"
LATE_BOUNDARY = "41000000-0000-4000-8000-000000000004"
DELIVERED = "41000000-0000-4000-8000-000000000005"
_DELIVERY = "gobby.hooks.terminal_handoff_delivery"
LONG_PAST = UNCONFIRMED_COMPACT_WINDOW + timedelta(hours=3)


def _attempt(session_id: str) -> str:
    return session_id.replace("-", "")


def _session(hub_db: HubDatabase, session_id: str, *, status: str = "active") -> None:
    if hub_db.fetchone("SELECT 1 FROM projects WHERE id = %s", (PROJECT_ID,)) is None:
        hub_db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_ID, "expiry"))
    hub_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, session_type, "
        "status) VALUES (%s, %s, %s, 'codex', %s, 'terminal', %s)",
        (session_id, session_id, MACHINE_ID, PROJECT_ID, status),
    )


def _claim(hub_db: HubDatabase, session_id: str) -> ClaimedHandoffDelivery:
    attempt_id = _attempt(session_id)
    stage_handoff_attempt(
        hub_db,
        session_id,
        attempt_id=attempt_id,
        handoff=build_handoff_payload(current_state="working", next_steps=["continue"]),
        clear_session=False,
    )
    SessionVariableManager(hub_db).merge_variables(
        session_id,
        {
            HANDOFF_DISPATCH_GATE_VARIABLE: {
                "handoff_staged": True,
                "delivery_pending": True,
                "attempt_id": attempt_id,
                "clear_session": False,
            }
        },
    )
    claimed = claim_staged_handoff_delivery(hub_db, session_id, attempt_id)
    assert claimed is not None
    return claimed


def _age(hub_db: HubDatabase, session_id: str, age: timedelta) -> datetime:
    row = hub_db.fetchone(
        "UPDATE session_handoffs SET authored_at = now() - %s WHERE session_id = %s "
        "RETURNING authored_at",
        (age, session_id),
    )
    assert row is not None
    authored: datetime = row["authored_at"]
    return authored


def _unconfirmed(hub_db: HubDatabase, session_id: str, *, age: timedelta) -> datetime:
    """Fail one compact attempt the way a boundary timeout does, authored ``age`` ago."""
    claimed = _claim(hub_db, session_id)
    failure = _compensate_delivery_failure(
        hub_db,
        claimed,
        "compact boundary was not observed before the confirmation deadline",
        error_code="compact_unconfirmed",
    )
    assert failure is not None
    SessionVariableManager(hub_db).set_variable(
        session_id,
        HANDOFF_COMPACT_CONTINUE_VARIABLE,
        {"attempt_id": claimed.attempt_id, "prompt": "Call get_handoff"},
    )
    return _age(hub_db, session_id, age)


def _variables(hub_db: HubDatabase, session_id: str) -> dict[str, Any]:
    return SessionVariableManager(hub_db).get_variables(session_id)


def _receipts(hub_db: HubDatabase, session_id: str) -> int:
    row = hub_db.fetchone(
        "SELECT count(*) AS n FROM session_handoff_deliveries WHERE attempt_id = %s",
        (_attempt(session_id),),
    )
    assert row is not None
    return int(row["n"])


def _app(dispatcher: SimpleNamespace) -> Any:
    return patch(
        f"{_DELIVERY}.get_app_context", return_value=SimpleNamespace(wake_dispatcher=dispatcher)
    )


def _dispatcher() -> SimpleNamespace:
    return SimpleNamespace(wake=AsyncMock(return_value=True))


def _completion_ids(dispatcher: SimpleNamespace) -> list[str]:
    return [str(call.args[2]["completion_id"]) for call in dispatcher.wake.await_args_list]


async def _drain(before: set[asyncio.Task[Any]]) -> int:
    """Await the release tasks scheduled since ``before`` and return how many there were."""
    await asyncio.sleep(0)
    owned = set(_HANDOFF_COMPACT_CONTINUATION_TASKS) - before
    await asyncio.wait_for(asyncio.gather(*owned), timeout=10)
    return len(owned)


async def _run_operation[T](
    _run_id: str, operation: Callable[[], Awaitable[T]], **_kwargs: Any
) -> T:
    return await operation()


async def test_release_waits_out_the_window_then_releases_and_wakes_once(
    hub_db: HubDatabase,
) -> None:
    _session(hub_db, STRANDED)
    _unconfirmed(hub_db, STRANDED, age=timedelta(minutes=1))
    delays: list[float] = []

    async def window_closes(delay: float) -> None:
        delays.append(delay)
        _age(hub_db, STRANDED, UNCONFIRMED_COMPACT_WINDOW + timedelta(minutes=1))

    dispatcher = _dispatcher()
    deliver = AsyncMock()
    with (
        patch(f"{_DELIVERY}.asyncio.sleep", new=window_closes),
        patch(f"{_DELIVERY}.deliver_staged_compact_handoff", new=deliver),
        _app(dispatcher),
    ):
        await _release_expired_compact(hub_db, STRANDED, _attempt(STRANDED))
        await _release_expired_compact(hub_db, STRANDED, _attempt(STRANDED))

    remaining = (UNCONFIRMED_COMPACT_WINDOW - timedelta(minutes=1)).total_seconds()
    assert delays == [pytest.approx(remaining + 1.0, abs=5.0)]
    deliver.assert_not_called()
    dispatcher.wake.assert_awaited_once()
    session_id, message, metadata = dispatcher.wake.await_args.args
    assert session_id == STRANDED
    assert metadata["completion_id"] == f"handoff-expired:{_attempt(STRANDED)}"
    assert "gobby-sessions:set_handoff" in message
    assert f"failed_attempt_id={_attempt(STRANDED)!r}" in message
    variables = _variables(hub_db, STRANDED)
    assert HANDOFF_DISPATCH_GATE_VARIABLE not in variables
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in variables
    assert variables[FAILED_HANDOFF_VARIABLE]["attempt_id"] == _attempt(STRANDED)
    assert _receipts(hub_db, STRANDED) == 0


async def test_release_stops_after_its_second_settle(hub_db: HubDatabase) -> None:
    _session(hub_db, STRANDED)
    _unconfirmed(hub_db, STRANDED, age=timedelta(minutes=1))
    delays: list[float] = []

    async def clock_lags(delay: float) -> None:
        delays.append(delay)

    dispatcher = _dispatcher()
    with patch(f"{_DELIVERY}.asyncio.sleep", new=clock_lags), _app(dispatcher):
        await _release_expired_compact(hub_db, STRANDED, _attempt(STRANDED))

    assert len(delays) == 1
    dispatcher.wake.assert_not_awaited()
    assert _variables(hub_db, STRANDED)[HANDOFF_DISPATCH_GATE_VARIABLE]["error_code"] == (
        "compact_unconfirmed"
    )


async def _settle_long_past_attempt(
    hub_db: HubDatabase, dispatcher: SimpleNamespace, error_code: str
) -> int:
    """Fail an attempt authored long ago through the live dispatch path; drain its release."""
    _session(hub_db, STRANDED)
    claimed = _claim(hub_db, STRANDED)
    _age(hub_db, STRANDED, LONG_PAST)
    before = set(_HANDOFF_COMPACT_CONTINUATION_TASKS)
    with (
        patch(
            f"{_DELIVERY}.deliver_staged_compact_handoff",
            new=AsyncMock(
                return_value={
                    "compacted": False,
                    "reason": "boundary not observed",
                    "error_code": error_code,
                }
            ),
        ),
        patch(f"{_DELIVERY}.shielded_terminal_delivery", side_effect=_run_operation),
        _app(dispatcher),
    ):
        await terminal_handoff_delivery._settle_delivery(
            claimed,
            session_manager=SessionManager(hub_db),
            agent_run_manager=MagicMock(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        )
        return await _drain(before)


@pytest.mark.parametrize(
    ("error_code", "released"), [("compact_unconfirmed", True), ("compact_failed", False)]
)
async def test_failed_compact_owns_one_release_only_when_unconfirmed(
    hub_db: HubDatabase, error_code: str, released: bool
) -> None:
    dispatcher = _dispatcher()

    owned = await _settle_long_past_attempt(hub_db, dispatcher, error_code)

    attempt_id = _attempt(STRANDED)
    expected = [f"handoff-failed:{attempt_id}"]
    if released:
        expected.append(f"handoff-expired:{attempt_id}")
    assert (owned, _completion_ids(dispatcher)) == (int(released), expected)
    assert (HANDOFF_DISPATCH_GATE_VARIABLE in _variables(hub_db, STRANDED)) is not released
    assert _receipts(hub_db, STRANDED) == 0


async def test_release_is_owned_before_the_failure_wake(hub_db: HubDatabase) -> None:
    dispatcher = SimpleNamespace(wake=AsyncMock(side_effect=[RuntimeError("wake refused"), True]))

    owned = await _settle_long_past_attempt(hub_db, dispatcher, "compact_unconfirmed")

    attempt_id = _attempt(STRANDED)
    assert owned == 1
    assert _completion_ids(dispatcher) == [
        f"handoff-failed:{attempt_id}",
        f"handoff-expired:{attempt_id}",
    ]
    assert HANDOFF_DISPATCH_GATE_VARIABLE not in _variables(hub_db, STRANDED)


async def test_reclaimed_dead_dispatch_owns_its_release(
    hub_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Stop-time reclaim settles a long-dead dispatch outside any startup sweep."""
    _session(hub_db, STRANDED)
    _claim(hub_db, STRANDED)
    _age(hub_db, STRANDED, LONG_PAST)
    variables = SessionVariableManager(hub_db)
    marker = variables.get_variables(STRANDED)[PENDING_HANDOFF_VARIABLE]
    stale = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    variables.merge_variables(
        STRANDED, {PENDING_HANDOFF_VARIABLE: {**marker, "dispatch_started_at": stale}}
    )
    monkeypatch.setattr("gobby.sessions.handoff.DISPATCH_OWNER", "f" * 32)
    monkeypatch.setattr(f"{_DELIVERY}.DISPATCH_OWNER", "f" * 32)
    dispatcher = _dispatcher()
    before = set(_HANDOFF_COMPACT_CONTINUATION_TASKS)

    with _app(dispatcher):
        resumed = await asyncio.to_thread(
            resume_dead_handoff_dispatches,
            MACHINE_ID,
            session_manager=SessionManager(hub_db),
            agent_run_manager=MagicMock(),
            event_loop=asyncio.get_running_loop(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        )
        owned = await _drain(before)

    assert (resumed, owned) == (0, 1)
    assert _completion_ids(dispatcher) == [f"handoff-expired:{_attempt(STRANDED)}"]
    assert HANDOFF_DISPATCH_GATE_VARIABLE not in _variables(hub_db, STRANDED)
    assert _receipts(hub_db, STRANDED) == 0


def _seat_fleet(hub_db: HubDatabase) -> None:
    """One stranded seat beside four that the sweep must leave alone."""
    _session(hub_db, STRANDED, status="paused")
    _unconfirmed(hub_db, STRANDED, age=LONG_PAST)
    _session(hub_db, EXPIRED)
    _unconfirmed(hub_db, EXPIRED, age=LONG_PAST)
    hub_db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (EXPIRED,))
    _session(hub_db, SUPERSEDED)
    _unconfirmed(hub_db, SUPERSEDED, age=LONG_PAST)
    SessionVariableManager(hub_db).set_variable(
        SUPERSEDED, PENDING_HANDOFF_VARIABLE, {"attempt_id": "f" * 32}
    )
    _session(hub_db, LATE_BOUNDARY)
    authored = _unconfirmed(hub_db, LATE_BOUNDARY, age=LONG_PAST)
    SessionVariableManager(hub_db).set_variable(
        LATE_BOUNDARY,
        COMPACT_NOTIFICATION_STARTED_AT_VARIABLE,
        (authored + timedelta(minutes=5)).isoformat(),
    )
    _session(hub_db, DELIVERED)
    claimed = _claim(hub_db, DELIVERED)
    record_handoff_delivery(
        hub_db,
        handoff_id=claimed.handoff_record_id,
        attempt_id=claimed.attempt_id,
        boundary_kind="compact",
        continuation_session_id=DELIVERED,
    )
    SessionVariableManager(hub_db).set_variable(
        DELIVERED,
        HANDOFF_DISPATCH_GATE_VARIABLE,
        {
            "attempt_id": claimed.attempt_id,
            "clear_session": False,
            "delivery_failed": True,
            "error_code": "compact_unconfirmed",
            "readiness_unconfirmed": True,
        },
    )
    _age(hub_db, DELIVERED, LONG_PAST)


async def _sweep(hub_db: HubDatabase) -> int:
    before = set(_HANDOFF_COMPACT_CONTINUATION_TASKS)
    armed = await asyncio.to_thread(
        resume_unconfirmed_compact_expiries,
        MACHINE_ID,
        hub_db,
        event_loop=asyncio.get_running_loop(),
    )
    assert await _drain(before) == armed
    return armed


async def test_startup_sweep_releases_a_long_stranded_seat_once(hub_db: HubDatabase) -> None:
    _seat_fleet(hub_db)
    dispatcher = _dispatcher()

    with _app(dispatcher):
        first = await _sweep(hub_db)
        second = await _sweep(hub_db)

    assert (first, second) == (4, 3)
    assert _completion_ids(dispatcher) == [f"handoff-expired:{_attempt(STRANDED)}"]
    assert dispatcher.wake.await_args.args[0] == STRANDED
    assert HANDOFF_DISPATCH_GATE_VARIABLE not in _variables(hub_db, STRANDED)
    assert _receipts(hub_db, STRANDED) == 0
    for kept in (EXPIRED, SUPERSEDED, LATE_BOUNDARY, DELIVERED):
        gate = _variables(hub_db, kept)[HANDOFF_DISPATCH_GATE_VARIABLE]
        assert (kept, gate["attempt_id"], gate["delivery_failed"]) == (
            kept,
            _attempt(kept),
            True,
        )

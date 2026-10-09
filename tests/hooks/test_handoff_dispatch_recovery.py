"""A handoff dispatch killed after its claim recovers exactly once."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.events.wake import WakeDispatcher
from gobby.hooks import terminal_handoff_delivery
from gobby.hooks.terminal_handoff_delivery import resume_dead_handoff_dispatches
from gobby.sessions import codex_compact_watch
from gobby.sessions.clear_continuation import stage_clear_attempt
from gobby.sessions.compact_continuation import (
    _HANDOFF_COMPACT_CONTINUATION_TASKS,
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
    arm_compact_boundary_waiter,
    compact_boundary_wait_submitted,
    mark_handoff_compact_continuation_pending,
    notify_compact_boundary,
    register_compact_boundary_waiter,
    unregister_compact_boundary_waiter,
)
from gobby.sessions.compact_markers import COMPACT_NOTIFICATION_STARTED_AT_VARIABLE
from gobby.sessions.handoff import (
    HANDOFF_DISPATCH_GATE_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    ClaimedHandoffDelivery,
    build_handoff_continue_prompt,
    claim_staged_handoff_delivery,
    consume_pending_handoff,
    recover_failed_handoff,
    stage_handoff_attempt,
    staged_handoff_rejection,
)
from gobby.sessions.handoff_records import build_handoff_payload, record_handoff_delivery
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"
MACHINE_ID = "21000000-0000-4000-8000-000000000001"
ATTEMPT_ID = "a" * 32
_DELIVERY = "gobby.hooks.terminal_handoff_delivery"
_COMPACT_DELIVERY = "gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery"
_RECOVERY = "gobby.hooks.handoff_dispatch_recovery"
_TERMINAL_CONTEXT = {"parent_pid": 12364, "gobby_session_id": SESSION_ID}


def _session_manager(
    hub_db: HubDatabase, source: str, *, transcript_path: Path | None = None
) -> SessionManager:
    hub_db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_ID, "recovery"))
    hub_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, session_type, "
        "terminal_context, transcript_path) VALUES (%s, %s, %s, %s, %s, 'terminal', %s, %s)",
        (
            SESSION_ID,
            SESSION_ID,
            MACHINE_ID,
            source,
            PROJECT_ID,
            json.dumps(_TERMINAL_CONTEXT),
            str(transcript_path) if transcript_path else None,
        ),
    )
    return SessionManager(hub_db)


def _claim(hub_db: HubDatabase, *, clear_session: bool = False) -> ClaimedHandoffDelivery:
    handoff = build_handoff_payload(current_state="working", next_steps=["continue"])
    if clear_session:
        stage_clear_attempt(
            hub_db,
            SESSION_ID,
            attempt_id=ATTEMPT_ID,
            handoff=handoff,
            terminal_context=_TERMINAL_CONTEXT,
            chat_context=None,
        )
    else:
        stage_handoff_attempt(
            hub_db,
            SESSION_ID,
            attempt_id=ATTEMPT_ID,
            handoff=handoff,
            clear_session=False,
        )
    SessionVariableManager(hub_db).merge_variables(
        SESSION_ID,
        {
            HANDOFF_DISPATCH_GATE_VARIABLE: {
                "handoff_staged": True,
                "delivery_pending": True,
                "attempt_id": ATTEMPT_ID,
                "clear_session": clear_session,
            }
        },
    )
    claimed = claim_staged_handoff_delivery(hub_db, SESSION_ID, ATTEMPT_ID)
    assert claimed is not None
    assert claimed.reclaimed_dispatch_started_at is None
    return claimed


def _restart_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """The claiming process died; its successor has a new dispatch owner."""
    monkeypatch.setattr("gobby.sessions.handoff.DISPATCH_OWNER", "f" * 32)
    monkeypatch.setattr(f"{_DELIVERY}.DISPATCH_OWNER", "f" * 32)


def _sweep(session_manager: SessionManager, loop: Any) -> int:
    return resume_dead_handoff_dispatches(
        MACHINE_ID,
        session_manager=session_manager,
        agent_run_manager=MagicMock(),
        event_loop=loop,
        terminal_manager=None,
        terminal_runtime_registry=None,
    )


def _receipts(hub_db: HubDatabase, kind: str = "compact") -> int:
    row = hub_db.fetchone(
        "SELECT count(*) AS n FROM session_handoff_deliveries "
        "WHERE attempt_id = %s AND boundary_kind = %s",
        (ATTEMPT_ID, kind),
    )
    assert row is not None
    return int(row["n"])


async def _run_operation[T](
    _run_id: str, operation: Callable[[], Awaitable[T]], **_kwargs: Any
) -> T:
    return await operation()


def test_live_claim_in_this_process_stays_exclusive(hub_db: HubDatabase) -> None:
    _session_manager(hub_db, "claude")
    _claim(hub_db)

    assert claim_staged_handoff_delivery(hub_db, SESSION_ID, ATTEMPT_ID) is None
    reason = staged_handoff_rejection(
        SessionVariableManager(hub_db).get_variables(SESSION_ID), ATTEMPT_ID
    )
    assert reason is not None
    assert reason.startswith("dispatch already started at ")


@pytest.mark.parametrize("source", ["claude", "codex"])
async def test_dispatch_killed_after_claim_redelivers_exactly_once(
    hub_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    session_manager = _session_manager(hub_db, source)
    _claim(hub_db)
    _restart_daemon(monkeypatch)

    with patch(f"{_DELIVERY}.asyncio.run_coroutine_threadsafe") as submit:
        resumed = _sweep(session_manager, asyncio.get_running_loop())
        resumed_again = _sweep(session_manager, asyncio.get_running_loop())

    assert (resumed, resumed_again) == (1, 0)
    submit.assert_called_once()
    marker = SessionVariableManager(hub_db).get_variables(SESSION_ID)[PENDING_HANDOFF_VARIABLE]
    assert marker["dispatch_owner"] == "f" * 32

    pane = SimpleNamespace(backend="native", snapshot=AsyncMock(return_value="Compacting..."))
    submissions = 0

    async def send_command(*_args: Any, **kwargs: Any) -> tuple[bool, None, bool, None]:
        nonlocal submissions
        submissions += 1
        kwargs["on_command_submitting"]()
        kwargs["mark_continuation_pending"]()
        notify_compact_boundary(hub_db, SESSION_ID, _TERMINAL_CONTEXT)
        return True, None, True, None

    with (
        patch(f"{_COMPACT_DELIVERY}._resolve_pane_io", return_value=(pane, None)),
        patch(f"{_COMPACT_DELIVERY}._interrupt_observer", return_value=(None, None)),
        patch(f"{_COMPACT_DELIVERY}._turn_settled_observer", return_value=None),
        patch(f"{_COMPACT_DELIVERY}._send_terminal_compaction_command", side_effect=send_command),
        patch(f"{_COMPACT_DELIVERY}.composer_reader", return_value=None),
        patch(f"{_COMPACT_DELIVERY}.clear_queued_context"),
        patch(
            f"{_COMPACT_DELIVERY}.schedule_codex_handoff_compact_continuation_readiness",
            return_value=True,
        ),
        patch(f"{_DELIVERY}.shielded_terminal_delivery", side_effect=_run_operation),
        patch(
            f"{_DELIVERY}.confirm_reclaimed_compact",
            new=AsyncMock(return_value={"compacted": False, "reason": "confirm-only path"}),
        ) as confirm,
    ):
        await submit.call_args.args[0]

    assert submissions == 1
    confirm.assert_not_awaited()
    assert _receipts(hub_db) == 1
    variables = SessionVariableManager(hub_db).get_variables(SESSION_ID)
    assert variables[HANDOFF_DISPATCH_GATE_VARIABLE].get("delivery_failed") is not True
    consumed = consume_pending_handoff(hub_db, SESSION_ID)
    assert consumed is not None
    assert "working" in consumed.markdown
    assert consume_pending_handoff(hub_db, SESSION_ID) is None
    assert _receipts(hub_db) == 1


def _codex_rollout(path: Path, *compacted_at: datetime) -> Path:
    lines = [{"type": "session_meta", "payload": {"id": SESSION_ID}}]
    lines += [{"type": "compacted", "timestamp": at.isoformat()} for at in compacted_at]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


@pytest.mark.parametrize("source", ["claude", "codex"])
def test_dead_dispatch_whose_compact_landed_continues_without_resubmitting(
    hub_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    session_manager = _session_manager(hub_db, source, transcript_path=rollout)
    _claim(hub_db)
    landed_at = datetime.now(UTC)
    _codex_rollout(rollout, landed_at)
    if source == "claude":
        SessionVariableManager(hub_db).merge_variables(
            SESSION_ID, {COMPACT_NOTIFICATION_STARTED_AT_VARIABLE: landed_at.isoformat()}
        )
    _restart_daemon(monkeypatch)
    loop = MagicMock()
    loop.is_closed.return_value = False

    with (
        patch(f"{_DELIVERY}.asyncio.run_coroutine_threadsafe") as submit,
        patch(
            "gobby.sessions.compact_continuation.schedule_handoff_compact_continuation",
            return_value=True,
        ) as continuation,
    ):
        assert _sweep(session_manager, loop) == 1

    submit.assert_not_called()
    assert _receipts(hub_db) == 1
    continuation.assert_called_once()
    assert continuation.call_args.args[1] == build_handoff_continue_prompt()
    consumed = consume_pending_handoff(hub_db, SESSION_ID)
    assert consumed is not None
    assert _receipts(hub_db) == 1


def test_dead_compact_dispatch_past_the_in_flight_window_fails_for_reconciliation(
    hub_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_manager = _session_manager(hub_db, "claude")
    _claim(hub_db)
    variables = SessionVariableManager(hub_db)
    marker = variables.get_variables(SESSION_ID)[PENDING_HANDOFF_VARIABLE]
    stale = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    variables.merge_variables(
        SESSION_ID, {PENDING_HANDOFF_VARIABLE: {**marker, "dispatch_started_at": stale}}
    )
    _restart_daemon(monkeypatch)
    loop = MagicMock()
    loop.is_closed.return_value = False

    with patch(f"{_DELIVERY}.asyncio.run_coroutine_threadsafe") as submit:
        assert _sweep(session_manager, loop) == 0

    submit.assert_not_called()
    current = variables.get_variables(SESSION_ID)
    assert PENDING_HANDOFF_VARIABLE not in current
    assert current[HANDOFF_DISPATCH_GATE_VARIABLE]["delivery_failed"] is True
    assert current[HANDOFF_DISPATCH_GATE_VARIABLE]["error_code"] == "compact_unconfirmed"


def _kill_after_precompact(
    hub_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, source: str, rollout: Path
) -> SessionManager:
    """The dispatch typed the compact and PreCompact fired, then the daemon died."""
    session_manager = _session_manager(hub_db, source, transcript_path=rollout)
    _claim(hub_db)
    # Marked when the dead dispatch typed the compact, so past its freshness window now.
    mark_handoff_compact_continuation_pending(
        hub_db, SESSION_ID, attempt_id=ATTEMPT_ID, now=datetime.now(UTC) - timedelta(hours=1)
    )
    session_manager.update_session_status(SESSION_ID, "awaiting_handoff")
    _restart_daemon(monkeypatch)
    monkeypatch.setattr(f"{_RECOVERY}._COMPACT_BOUNDARY_POLL_SECONDS", 0.01)
    return session_manager


async def _confirm_reclaimed(session_manager: SessionManager, land: Any) -> None:
    """Sweep the dead claim, land the boundary once its wait is armed, drain the wait."""
    with patch(f"{_DELIVERY}.asyncio.run_coroutine_threadsafe") as submit:
        assert _sweep(session_manager, asyncio.get_running_loop()) == 1
    with (
        patch(f"{_COMPACT_DELIVERY}._send_terminal_compaction_command") as resubmit,
        patch(f"{_DELIVERY}.shielded_terminal_delivery", side_effect=_run_operation),
    ):
        confirm = asyncio.create_task(submit.call_args.args[0])
        for _ in range(500):
            if compact_boundary_wait_submitted(SESSION_ID) or confirm.done():
                break
            await asyncio.sleep(0.01)
        if land is not None:
            land()
        await asyncio.wait_for(confirm, timeout=5)
    resubmit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["claude", "codex"])
async def test_dispatch_killed_after_precompact_confirms_its_boundary_once(
    hub_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str
) -> None:
    rollout = _codex_rollout(tmp_path / "rollout.jsonl")
    session_manager = _kill_after_precompact(hub_db, monkeypatch, source, rollout)

    def land() -> None:
        if source == "codex":
            _append_compacted(rollout)
        else:
            notify_compact_boundary(hub_db, SESSION_ID, _TERMINAL_CONTEXT)

    with patch(
        "gobby.sessions.compact_continuation.schedule_handoff_compact_continuation",
        return_value=True,
    ) as continuation:
        await _confirm_reclaimed(session_manager, land)
        assert _sweep(session_manager, asyncio.get_running_loop()) == 0

    assert _receipts(hub_db) == 1
    variables = SessionVariableManager(hub_db).get_variables(SESSION_ID)
    assert variables[HANDOFF_DISPATCH_GATE_VARIABLE].get("delivery_failed") is not True
    if source == "codex":
        # Codex has no compact SessionStart, so the reclaimed wait sends the pull prompt.
        continuation.assert_called_once()
        assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in variables
    else:
        continuation.assert_not_called()
    current = session_manager.get(SESSION_ID)
    assert current is not None
    # A Claude compact's SessionStart moves the row on; Codex has none, so the wait releases it.
    assert current.status == ("paused" if source == "codex" else "awaiting_handoff")
    consumed = consume_pending_handoff(hub_db, SESSION_ID)
    assert consumed is not None
    assert consume_pending_handoff(hub_db, SESSION_ID) is None
    assert _receipts(hub_db) == 1


@pytest.mark.asyncio
async def test_dispatch_killed_after_precompact_fails_when_no_boundary_lands_by_its_deadline(
    hub_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rollout = _codex_rollout(tmp_path / "rollout.jsonl")
    session_manager = _kill_after_precompact(hub_db, monkeypatch, "codex", rollout)
    variables = SessionVariableManager(hub_db)
    marker = variables.get_variables(SESSION_ID)[PENDING_HANDOFF_VARIABLE]
    # Past the live confirmation deadline, still inside the in-flight window.
    started = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
    variables.merge_variables(
        SESSION_ID, {PENDING_HANDOFF_VARIABLE: {**marker, "dispatch_started_at": started}}
    )

    await _confirm_reclaimed(session_manager, None)

    assert _receipts(hub_db) == 0
    gate = variables.get_variables(SESSION_ID)[HANDOFF_DISPATCH_GATE_VARIABLE]
    assert gate["delivery_failed"] is True
    assert gate["error_code"] == "compact_unconfirmed"
    current = session_manager.get(SESSION_ID)
    assert current is not None
    assert current.status == "paused"
    assert _sweep(session_manager, asyncio.get_running_loop()) == 0


@pytest.mark.parametrize("cleared", [False, True])
def test_dead_clear_dispatch_redelivers_only_without_a_clear_receipt(
    hub_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, cleared: bool
) -> None:
    session_manager = _session_manager(hub_db, "claude")
    claimed = _claim(hub_db, clear_session=True)
    if cleared:
        record_handoff_delivery(
            hub_db,
            handoff_id=claimed.handoff_record_id,
            attempt_id=ATTEMPT_ID,
            boundary_kind="clear",
            continuation_session_id=SESSION_ID,
        )
    _restart_daemon(monkeypatch)
    loop = MagicMock()
    loop.is_closed.return_value = False

    with patch(f"{_DELIVERY}.asyncio.run_coroutine_threadsafe") as submit:
        assert _sweep(session_manager, loop) == 1

    assert submit.call_count == (0 if cleared else 1)
    if submit.called:
        submit.call_args.args[0].close()
    assert _receipts(hub_db, "clear") == (1 if cleared else 0)


async def test_wake_leaves_the_composer_to_a_staged_handoff_dispatch(
    hub_db: HubDatabase,
) -> None:
    session_manager = _session_manager(hub_db, "codex")
    session_manager.update_session_status(SESSION_ID, "paused")
    dispatcher = WakeDispatcher(session_manager=session_manager, ism_manager=MagicMock())
    _claim(hub_db)

    _session, skipped = await dispatcher._preflight_live_side_effect(SESSION_ID)

    assert skipped == {
        "session_id": SESSION_ID,
        "delivered": False,
        "method": "next_call_context",
        "skipped": "handoff_delivery_pending",
        "ism_persisted": True,
    }
    assert consume_pending_handoff(hub_db, SESSION_ID) is not None
    _session, skipped = await dispatcher._preflight_live_side_effect(SESSION_ID)
    assert skipped is None


async def _drain_watch() -> None:
    await asyncio.wait_for(asyncio.gather(*_HANDOFF_COMPACT_CONTINUATION_TASKS), timeout=5)


def _append_compacted(rollout: Path) -> None:
    record = {"type": "compacted", "timestamp": datetime.now(UTC).isoformat()}
    with rollout.open("a") as stream:
        stream.write(json.dumps(record) + "\n")


@pytest.mark.parametrize("pending", [True, False])
async def test_out_of_band_codex_compact_continues_and_releases_awaiting_handoff(
    hub_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pending: bool
) -> None:
    rollout = _codex_rollout(tmp_path / "rollout.jsonl")
    session_manager = _session_manager(hub_db, "codex", transcript_path=rollout)
    session_manager.update_session_status(SESSION_ID, "awaiting_handoff")
    if pending:
        # A continuation left by a timed-out dispatch is already past its freshness window.
        mark_handoff_compact_continuation_pending(
            hub_db,
            SESSION_ID,
            attempt_id=ATTEMPT_ID,
            now=datetime.now(UTC) - timedelta(hours=1),
        )
    monkeypatch.setattr(codex_compact_watch, "CODEX_COMPACT_WATCH_POLL_SECONDS", 0.01)
    session = session_manager.get(SESSION_ID)

    with patch(
        "gobby.sessions.compact_continuation.schedule_handoff_compact_continuation",
        return_value=True,
    ) as continuation:
        assert codex_compact_watch.watch_codex_out_of_band_compact(
            hub_db,
            session,
            loop=None,
            terminal_manager=None,
            terminal_runtime_registry=None,
        )
        _append_compacted(rollout)
        await _drain_watch()

    current = session_manager.get(SESSION_ID)
    assert current is not None
    assert current.status == "paused"
    if pending:
        continuation.assert_called_once()
        assert continuation.call_args.args[1] == build_handoff_continue_prompt()
    else:
        continuation.assert_not_called()
    variables = SessionVariableManager(hub_db).get_variables(SESSION_ID)
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in variables


async def test_dispatched_codex_compact_is_left_to_its_dispatch(
    hub_db: HubDatabase, tmp_path: Path
) -> None:
    rollout = _codex_rollout(tmp_path / "rollout.jsonl")
    session_manager = _session_manager(hub_db, "codex", transcript_path=rollout)
    register_compact_boundary_waiter(SESSION_ID, ATTEMPT_ID, "handoff-1", _TERMINAL_CONTEXT)
    try:
        arm_compact_boundary_waiter(SESSION_ID, ATTEMPT_ID)
        assert not codex_compact_watch.watch_codex_out_of_band_compact(
            hub_db,
            session_manager.get(SESSION_ID),
            loop=None,
            terminal_manager=None,
            terminal_runtime_registry=None,
        )
    finally:
        unregister_compact_boundary_waiter(SESSION_ID, ATTEMPT_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["claude", "codex"])
@pytest.mark.parametrize("failure", ["result", "exception"])
@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.parametrize("cancellations", [1, 2])
async def test_caller_cancellation_preserves_failed_handoff_recovery(
    hub_db: HubDatabase, source: str, failure: str, clear: bool, cancellations: int
) -> None:
    """The original shield must settle real failure compensation before cancellation."""
    session_manager = _session_manager(hub_db, source)
    claimed = _claim(hub_db, clear_session=clear)
    staged = asyncio.Event()
    release = asyncio.Event()
    physical_done = asyncio.Event()

    async def delivery(*args: Any, **kwargs: Any) -> dict[str, Any]:
        staged.set()
        await release.wait()
        physical_done.set()
        if failure == "exception":
            raise RuntimeError("owned transport failed")
        return {"compacted": False, "reason": "owned transport failed"}

    target = "deliver_staged_clear_session" if clear else "deliver_staged_compact_handoff"
    with patch.object(terminal_handoff_delivery, target, delivery):
        caller = asyncio.create_task(
            terminal_handoff_delivery._settle_delivery(
                claimed,
                session_manager=session_manager,
                agent_run_manager=MagicMock(),
                terminal_manager=None,
                terminal_runtime_registry=None,
            )
        )
        try:
            await asyncio.wait_for(staged.wait(), 1)
            assert caller.cancel(), "cancel the actual delivery caller while its owned task waits"
            assert not physical_done.is_set()
            if cancellations == 2:
                fence: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                asyncio.get_running_loop().call_soon(fence.set_result, None)
                await fence
                assert not caller.done() and caller.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 1)
        finally:
            release.set()
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)

    assert physical_done.is_set()
    variables = SessionVariableManager(hub_db).get_variables(SESSION_ID)
    gate = variables[HANDOFF_DISPATCH_GATE_VARIABLE]
    assert gate.get("delivery_state") == "failed_not_deliverable"
    assert gate.get("attempt_id") == ATTEMPT_ID
    assert gate.get("delivery_pending") is False
    assert _receipts(hub_db) == 0, "cancelled failed delivery cannot manufacture a compact receipt"
    assert _receipts(hub_db, "clear") == 0
    recovered = recover_failed_handoff(hub_db, SESSION_ID, ATTEMPT_ID)
    assert recovered is not None and "working" in recovered
    assert recover_failed_handoff(hub_db, SESSION_ID, ATTEMPT_ID) == recovered
    assert _receipts(hub_db) == 0 and _receipts(hub_db, "clear") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["claude", "codex"])
@pytest.mark.parametrize("clear", [False, True])
async def test_owner_loop_failure_preserves_failed_handoff_recovery(
    hub_db: HubDatabase, source: str, clear: bool
) -> None:
    session_manager = _session_manager(hub_db, source)
    claimed = _claim(hub_db, clear_session=clear)
    with patch.object(
        terminal_handoff_delivery,
        "shielded_terminal_delivery",
        AsyncMock(side_effect=RuntimeError("owner loop unavailable")),
    ):
        await terminal_handoff_delivery._settle_delivery(
            claimed,
            session_manager=session_manager,
            agent_run_manager=MagicMock(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        )
    recovered = recover_failed_handoff(hub_db, SESSION_ID, ATTEMPT_ID)
    assert recovered is not None and "working" in recovered
    gate = SessionVariableManager(hub_db).get_variables(SESSION_ID)[HANDOFF_DISPATCH_GATE_VARIABLE]
    assert gate["delivery_pending"] is False
    assert gate["reason"] == "owner loop unavailable"
    assert _receipts(hub_db) == 0 and _receipts(hub_db, "clear") == 0

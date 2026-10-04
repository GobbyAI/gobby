"""A Codex seat whose compact handoff fails is left a working path forward (#23380)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.terminal_delivery import TerminalDeliveryAdmissionClosedError
from gobby.events.live_wake import handoff_delivery_skip
from gobby.hooks import terminal_handoff_delivery
from gobby.sessions.compact_continuation import (
    HANDOFF_COMPACT_CONTINUE_VARIABLE,
    _continue_after_codex_compaction_ready,
    mark_handoff_compact_continuation_pending,
)
from gobby.sessions.handoff import (
    HANDOFF_DISPATCH_GATE_VARIABLE,
    HANDOFF_TURN_END_PENDING_VARIABLE,
    ClaimedHandoffDelivery,
    build_handoff_continue_prompt,
    claim_staged_handoff_delivery,
    stage_handoff_attempt,
)
from gobby.sessions.handoff_records import build_handoff_payload
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"
MACHINE_ID = "21000000-0000-4000-8000-000000000001"
ATTEMPT_ID = "a" * 32
_DELIVERY = "gobby.hooks.terminal_handoff_delivery"
_COMPACT_DELIVERY = "gobby.mcp_proxy.tools.sessions._terminal_handoff_delivery"
_TERMINAL_CONTEXT = {"parent_pid": 12364, "gobby_session_id": SESSION_ID}
# Codex printed this on 2026-10-03 after /compact reached its still-running turn.
CODEX_REFUSAL = "/compact is disabled while a task is in progress"


def _session_manager(hub_db: HubDatabase) -> SessionManager:
    hub_db.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (PROJECT_ID, "refusal"))
    hub_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, session_type, "
        "terminal_context) VALUES (%s, %s, %s, 'codex', %s, 'terminal', %s)",
        (SESSION_ID, SESSION_ID, MACHINE_ID, PROJECT_ID, json.dumps(_TERMINAL_CONTEXT)),
    )
    manager = SessionManager(hub_db)
    manager.update_session_status(SESSION_ID, "active")
    return manager


def _claim(hub_db: HubDatabase) -> ClaimedHandoffDelivery:
    stage_handoff_attempt(
        hub_db,
        SESSION_ID,
        attempt_id=ATTEMPT_ID,
        handoff=build_handoff_payload(current_state="working", next_steps=["continue"]),
        clear_session=False,
    )
    SessionVariableManager(hub_db).merge_variables(
        SESSION_ID,
        {
            HANDOFF_DISPATCH_GATE_VARIABLE: {
                "handoff_staged": True,
                "delivery_pending": True,
                "attempt_id": ATTEMPT_ID,
                "clear_session": False,
            }
        },
    )
    claimed = claim_staged_handoff_delivery(hub_db, SESSION_ID, ATTEMPT_ID)
    assert claimed is not None
    return claimed


async def _run_operation[T](
    _run_id: str, operation: Callable[[], Awaitable[T]], **_kwargs: Any
) -> T:
    return await operation()


def _wake_dispatcher() -> SimpleNamespace:
    return SimpleNamespace(wake=AsyncMock(return_value={"delivered": True}))


async def _settle(
    claimed: ClaimedHandoffDelivery,
    session_manager: SessionManager,
    app: SimpleNamespace | None,
) -> None:
    with (
        patch(f"{_DELIVERY}.shielded_terminal_delivery", side_effect=_run_operation),
        patch(f"{_DELIVERY}.get_app_context", return_value=app),
    ):
        await terminal_handoff_delivery._settle_delivery(
            claimed,
            session_manager=session_manager,
            agent_run_manager=MagicMock(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        )


def _assert_seat_can_continue(hub_db: HubDatabase, session_manager: SessionManager) -> None:
    variables = SessionVariableManager(hub_db).get_variables(SESSION_ID)
    assert HANDOFF_TURN_END_PENDING_VARIABLE not in variables
    assert handoff_delivery_skip(SESSION_ID, variables) is None
    session = session_manager.get(SESSION_ID)
    assert session is not None
    assert session.status != "awaiting_handoff"


async def test_codex_refusal_fails_fast_and_wakes_the_seat_with_its_pull_prompt(
    hub_db: HubDatabase,
) -> None:
    session_manager = _session_manager(hub_db)
    claimed = _claim(hub_db)
    screen = ["• Working (12s • esc to interrupt)\n› "]
    pane = SimpleNamespace(backend="native", snapshot=AsyncMock(side_effect=lambda *_a: screen[0]))

    async def send_command(*_args: Any, **kwargs: Any) -> tuple[bool, None, bool, None]:
        # The sender saw no rejection in its window; Codex renders the refusal
        # afterwards while its turn keeps running, so no boundary will ever land.
        kwargs["on_command_submitting"]()
        kwargs["mark_continuation_pending"]()
        screen[0] += f"\n■ {CODEX_REFUSAL}\n• Working (14s • esc to interrupt)\n› "
        return True, None, True, None

    dispatcher = _wake_dispatcher()
    with (
        patch(f"{_COMPACT_DELIVERY}._resolve_pane_io", return_value=(pane, None)),
        patch(f"{_COMPACT_DELIVERY}._interrupt_observer", return_value=(None, None)),
        patch(f"{_COMPACT_DELIVERY}._turn_settled_observer", return_value=None),
        patch(f"{_COMPACT_DELIVERY}._send_terminal_compaction_command", side_effect=send_command),
        patch(f"{_COMPACT_DELIVERY}.composer_reader", return_value=None),
        patch(f"{_COMPACT_DELIVERY}._COMPACT_BOUNDARY_CONFIRM_SECONDS", 3.0),
        patch(f"{_COMPACT_DELIVERY}._COMPACT_BOUNDARY_POLL_SECONDS", 0.01),
    ):
        await _settle(claimed, session_manager, SimpleNamespace(wake_dispatcher=dispatcher))

    variables = SessionVariableManager(hub_db).get_variables(SESSION_ID)
    gate = variables[HANDOFF_DISPATCH_GATE_VARIABLE]
    assert gate["error_code"] == "compaction_command_rejected"
    assert gate["reason"] == CODEX_REFUSAL
    assert gate["delivery_failed"] is True
    assert HANDOFF_COMPACT_CONTINUE_VARIABLE not in variables
    _assert_seat_can_continue(hub_db, session_manager)
    dispatcher.wake.assert_awaited_once()
    session_id, message, result = dispatcher.wake.await_args.args
    assert session_id == SESSION_ID
    assert f"failed_attempt_id={ATTEMPT_ID!r}" in message
    assert result["attempt_id"] == ATTEMPT_ID
    assert result["error_code"] == "compaction_command_rejected"


async def test_readiness_timeout_without_receipt_leaves_the_seat_wakeable_and_woken(
    hub_db: HubDatabase,
) -> None:
    session_manager = _session_manager(hub_db)
    claimed = _claim(hub_db)
    assert mark_handoff_compact_continuation_pending(
        hub_db, SESSION_ID, prompt=build_handoff_continue_prompt(), attempt_id=ATTEMPT_ID
    )
    pane = SimpleNamespace(snapshot=AsyncMock(return_value="• Working\n› "))

    await _continue_after_codex_compaction_ready(
        hub_db,
        pane=pane,
        pending_session_id=SESSION_ID,
        before_command="• Working\n› ",
        poll_seconds=0,
        attempt_id=ATTEMPT_ID,
        fresh_seconds=0,
    )

    # Without a receipt the watcher defers to the dispatch, which still owns the composer.
    variables = SessionVariableManager(hub_db).get_variables(SESSION_ID)
    assert variables[HANDOFF_TURN_END_PENDING_VARIABLE] is True

    dispatcher = _wake_dispatcher()
    with patch(
        f"{_DELIVERY}.deliver_staged_compact_handoff",
        new=AsyncMock(
            return_value={
                "compacted": False,
                "reason": "provider boundary was not observed by deadline",
                "error_code": "compact_unconfirmed",
            }
        ),
    ):
        await _settle(claimed, session_manager, SimpleNamespace(wake_dispatcher=dispatcher))

    _assert_seat_can_continue(hub_db, session_manager)
    dispatcher.wake.assert_awaited_once()
    session_id, message, result = dispatcher.wake.await_args.args
    assert session_id == SESSION_ID
    assert f"failed_attempt_id={ATTEMPT_ID!r}" in message
    assert result["error_code"] == "compact_unconfirmed"


async def _settle_with_scope_failure(
    claimed: ClaimedHandoffDelivery,
    session_manager: SessionManager,
    app: SimpleNamespace,
    error: Exception,
) -> None:
    with (
        patch(f"{_DELIVERY}.shielded_terminal_delivery", new=AsyncMock(side_effect=error)),
        patch(f"{_DELIVERY}.get_app_context", return_value=app),
    ):
        await terminal_handoff_delivery._settle_delivery(
            claimed,
            session_manager=session_manager,
            agent_run_manager=MagicMock(),
            terminal_manager=None,
            terminal_runtime_registry=None,
        )


async def test_delivery_scope_failure_wakes_the_restored_seat(hub_db: HubDatabase) -> None:
    session_manager = _session_manager(hub_db)
    claimed = _claim(hub_db)
    dispatcher = _wake_dispatcher()

    await _settle_with_scope_failure(
        claimed,
        session_manager,
        SimpleNamespace(wake_dispatcher=dispatcher),
        RuntimeError("delivery scope broke"),
    )

    _assert_seat_can_continue(hub_db, session_manager)
    dispatcher.wake.assert_awaited_once()
    session_id, message, result = dispatcher.wake.await_args.args
    assert session_id == SESSION_ID
    assert f"failed_attempt_id={ATTEMPT_ID!r}" in message
    assert result["attempt_id"] == ATTEMPT_ID


async def test_admission_closed_at_shutdown_queues_the_pull_prompt_instead_of_waking(
    hub_db: HubDatabase,
) -> None:
    session_manager = _session_manager(hub_db)
    claimed = _claim(hub_db)
    dispatcher = _wake_dispatcher()

    await _settle_with_scope_failure(
        claimed,
        session_manager,
        SimpleNamespace(wake_dispatcher=dispatcher),
        TerminalDeliveryAdmissionClosedError("terminal delivery is not running"),
    )

    # The daemon is stopping, so a live wake would be lost; the durable prompt outlives it.
    _assert_seat_can_continue(hub_db, session_manager)
    dispatcher.wake.assert_not_awaited()
    queued = InterSessionMessageManager(hub_db).get_undelivered_messages(SESSION_ID)
    assert len(queued) == 1
    assert f"failed_attempt_id={ATTEMPT_ID!r}" in queued[0].content


async def test_failed_attempt_without_a_wake_dispatcher_queues_its_pull_prompt(
    hub_db: HubDatabase,
) -> None:
    session_manager = _session_manager(hub_db)
    claimed = _claim(hub_db)

    with patch(
        f"{_DELIVERY}.deliver_staged_compact_handoff",
        new=AsyncMock(return_value={"compacted": False, "reason": "pane disappeared"}),
    ):
        await _settle(claimed, session_manager, None)

    _assert_seat_can_continue(hub_db, session_manager)
    queued = InterSessionMessageManager(hub_db).get_undelivered_messages(SESSION_ID)
    assert len(queued) == 1
    assert queued[0].from_session == SESSION_ID
    assert f"failed_attempt_id={ATTEMPT_ID!r}" in queued[0].content

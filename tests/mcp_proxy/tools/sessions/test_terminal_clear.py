"""Clear-session delivery: pane routing, Codex thread-end gating, pending-attempt reuse."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.sessions import _terminal_clear
from gobby.sessions.clear_continuation import CLEAR_ATTEMPT_VARIABLE
from gobby.sessions.handoff import build_handoff_continue_prompt
from gobby.sessions.handoff_records import build_handoff_payload

_THREAD_ID = "01a0580a-b0c8-7552-aa18-8927ff248f85"
_THREAD_END_BANNER = f"To continue this session, run codex resume {_THREAD_ID}\n"
_IDLE_PANE = "› Ask Codex to do anything\n"
_HANDOFF = build_handoff_payload(current_state="Ready to continue.", next_steps=["Continue."])


def _terminal_session(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "id": "session-1",
        "external_id": _THREAD_ID,
        "status": "active",
        "source": "codex",
        "session_type": "terminal",
        "terminal_context": {
            "tmux_pane": "%1",
            "tmux_socket_path": "/tmp/tmux-test",
        },
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _Pane:
    """PaneIO fake: the pane text as captured; /clear delivery appends to it."""

    backend = "tmux"
    target = "%1"

    def __init__(self, text: str) -> None:
        self.text = text
        self.keys: list[str] = []

    async def send_key(self, key: str) -> tuple[bool, str | None]:
        self.keys.append(key)
        return True, None

    async def type_text(self, text: str) -> tuple[bool, str | None]:
        return True, None

    async def snapshot(self, lines: int = 12) -> str | None:
        assert lines > 0
        return self.text


def _base_patches(
    session: SimpleNamespace,
    pane: _Pane,
    *,
    restore_failed_attempt: Any,
    pending: dict[str, Any] | None = None,
) -> list[Any]:
    return [
        patch.object(_terminal_clear, "get_current_session_id", return_value=session.id),
        patch.object(
            _terminal_clear,
            "_resolve_session_for_compaction",
            return_value=(session.id, session, None),
        ),
        patch.object(
            _terminal_clear,
            "_authorize_send_keys_target",
            return_value=(session.id, None),
        ),
        patch.object(_terminal_clear, "_resolve_pane_io", return_value=(pane, None)),
        patch.object(_terminal_clear, "_interrupt_observer", return_value=(lambda: True, None)),
        patch.object(_terminal_clear, "pending_clear_attempt", return_value=pending),
        patch.object(_terminal_clear, "mark_clear_command_sent", return_value=True),
        patch.object(_terminal_clear, "stage_clear_attempt", return_value=MagicMock()),
        patch.object(_terminal_clear, "clear_failed_attempt", restore_failed_attempt),
    ]


async def _run_clear(patches: list[Any]) -> dict[str, Any]:
    agent_run_manager = MagicMock()
    agent_run_manager.get_by_session.return_value = None
    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        return await _terminal_clear.execute_clear_session(
            _HANDOFF,
            session_manager=MagicMock(),
            db=MagicMock(),
            agent_run_manager=agent_run_manager,
        )


@pytest.mark.asyncio
async def test_clear_delivery_survives_caller_cancellation() -> None:
    sender_started = asyncio.Event()
    release_sender = asyncio.Event()
    delivered_commands: list[str] = []

    async def send_command(
        _pane: Any,
        command: str,
        _session_id: str,
        **_kwargs: Any,
    ) -> tuple[bool, str | None, bool, dict[str, Any] | None]:
        sender_started.set()
        await release_sender.wait()
        delivered_commands.append(command)
        return True, None, True, None

    session = _terminal_session()
    agent_run_manager = MagicMock()
    agent_run_manager.get_by_session.return_value = None
    restore_failed_attempt = MagicMock(return_value=True)

    with ExitStack() as stack:
        for patcher in _base_patches(
            session, _Pane(_IDLE_PANE), restore_failed_attempt=restore_failed_attempt
        ):
            stack.enter_context(patcher)
        stack.enter_context(
            patch.object(_terminal_clear, "_send_terminal_compaction_command", send_command)
        )
        stack.enter_context(
            patch.object(
                _terminal_clear, "_wait_for_codex_thread_end", new=AsyncMock(return_value=True)
            )
        )
        stack.enter_context(
            patch.object(_terminal_clear, "schedule_handoff_continuation", return_value=True)
        )
        stack.enter_context(
            patch.object(
                _terminal_clear,
                "_wait_for_clear_acknowledgment",
                new=AsyncMock(return_value=("successor-1", "successor_binding")),
            )
        )
        caller = asyncio.create_task(
            _terminal_clear.execute_clear_session(
                _HANDOFF,
                session_manager=MagicMock(),
                db=MagicMock(),
                agent_run_manager=agent_run_manager,
            )
        )
        await sender_started.wait()

        caller.cancel()
        await asyncio.sleep(0)
        release_sender.set()

        with pytest.raises(asyncio.CancelledError):
            await caller

    assert delivered_commands == ["/clear"]
    restore_failed_attempt.assert_not_called()


@pytest.mark.asyncio
async def test_clear_delivery_failure_restores_staged_attempt() -> None:
    session = _terminal_session()
    pane = _Pane(_IDLE_PANE)
    restore_failed_attempt = MagicMock(return_value=True)
    stage_attempt = MagicMock(return_value=MagicMock())
    mark_sent = MagicMock(return_value=True)
    send_command = AsyncMock(return_value=(False, "delivery failed", False, None))

    patches = _base_patches(session, pane, restore_failed_attempt=restore_failed_attempt)
    patches = [
        p for p in patches if p.attribute not in {"stage_clear_attempt", "mark_clear_command_sent"}
    ]
    patches.extend(
        [
            patch.object(_terminal_clear, "stage_clear_attempt", stage_attempt),
            patch.object(_terminal_clear, "mark_clear_command_sent", mark_sent),
            patch.object(_terminal_clear, "_send_terminal_compaction_command", send_command),
        ]
    )
    result = await _run_clear(patches)

    assert result == {
        "success": False,
        "error": "delivery failed",
        "error_code": "clear_send_failed",
    }
    stage_attempt.assert_called_once()
    send_command.assert_awaited_once()
    await_args = send_command.await_args
    assert await_args is not None
    assert await_args.args[0] is pane
    assert await_args.args[1] == "/clear"
    restore_failed_attempt.assert_called_once()
    mark_sent.assert_not_called()


@pytest.mark.asyncio
async def test_clear_fails_closed_when_the_interrupt_cannot_be_observed() -> None:
    session = _terminal_session(source="claude")
    pane = _Pane("> ")
    restore_failed_attempt = MagicMock(return_value=True)
    stage_attempt = MagicMock(return_value=MagicMock())
    send_command = AsyncMock()
    observer = MagicMock(return_value=(None, "claude transcript unavailable"))

    patches = _base_patches(session, pane, restore_failed_attempt=restore_failed_attempt)
    patches = [
        p for p in patches if p.attribute not in {"_interrupt_observer", "stage_clear_attempt"}
    ]
    patches.extend(
        [
            patch.object(_terminal_clear, "_interrupt_observer", observer),
            patch.object(_terminal_clear, "stage_clear_attempt", stage_attempt),
            patch.object(_terminal_clear, "_send_terminal_compaction_command", send_command),
        ]
    )
    result = await _run_clear(patches)

    assert result == {
        "success": False,
        "error": "claude transcript unavailable",
        "error_code": "interrupt_observation_unavailable",
    }
    observer.assert_called_once_with("claude", session)
    assert pane.keys == []
    stage_attempt.assert_not_called()
    send_command.assert_not_awaited()
    restore_failed_attempt.assert_not_called()


def _clear_patches(
    session: SimpleNamespace,
    send_command: Any,
    *,
    pane: _Pane,
    acknowledgment: Any,
    restore_failed_attempt: MagicMock,
    schedule_continuation: MagicMock,
) -> list[Any]:
    patches = _base_patches(session, pane, restore_failed_attempt=restore_failed_attempt)
    patches.extend(
        [
            patch.object(_terminal_clear, "_send_terminal_compaction_command", send_command),
            patch.object(_terminal_clear, "schedule_handoff_continuation", schedule_continuation),
            patch.object(_terminal_clear, "_CODEX_CLEAR_BANNER_TIMEOUT_SECONDS", 0.2),
            patch.object(_terminal_clear, "_CODEX_CLEAR_BANNER_POLL_SECONDS", 0.01),
        ]
    )
    if acknowledgment is not None:
        patches.append(
            patch.object(_terminal_clear, "_wait_for_clear_acknowledgment", acknowledgment)
        )
    return patches


def _clear_that_ends_the_thread(pane: _Pane) -> Any:
    async def send_command(*_args: Any, **_kwargs: Any) -> tuple[bool, None, bool, None]:
        pane.text += _THREAD_END_BANNER + _IDLE_PANE
        return True, None, True, None

    return send_command


def _clear_that_leaves_the_pane() -> Any:
    async def send_command(*_args: Any, **_kwargs: Any) -> tuple[bool, None, bool, None]:
        return True, None, True, None

    return send_command


async def test_codex_clear_types_continuation_once_thread_end_banner_appears() -> None:
    session = _terminal_session()
    pane = _Pane(_IDLE_PANE)
    restore = MagicMock(return_value=True)
    schedule = MagicMock(return_value=True)
    acknowledgment = AsyncMock(return_value=("successor-1", "successor_binding"))

    result = await _run_clear(
        _clear_patches(
            session,
            _clear_that_ends_the_thread(pane),
            pane=pane,
            acknowledgment=acknowledgment,
            restore_failed_attempt=restore,
            schedule_continuation=schedule,
        ),
    )

    assert result["success"] is True
    assert result["acknowledged_by"] == "successor_binding"
    assert result["reused_attempt"] is False
    schedule.assert_called_once_with(
        session,
        build_handoff_continue_prompt(),
        delay_seconds=_terminal_clear._CODEX_CLEAR_CONTINUE_DELAY_SECONDS,
    )
    restore.assert_not_called()


async def test_codex_clear_without_thread_end_banner_types_continuation_once() -> None:
    """A successful /clear write advances even when Codex omits its legacy banner."""
    session = _terminal_session()
    pane = _Pane(_IDLE_PANE)
    restore = MagicMock(return_value=True)
    schedule = MagicMock(return_value=True)
    acknowledgment = AsyncMock(return_value=("successor-1", "successor_binding"))

    result = await _run_clear(
        _clear_patches(
            session,
            _clear_that_leaves_the_pane(),
            pane=pane,
            acknowledgment=acknowledgment,
            restore_failed_attempt=restore,
            schedule_continuation=schedule,
        ),
    )

    assert result["success"] is True
    assert result["acknowledged_by"] == "successor_binding"
    schedule.assert_called_once_with(
        session,
        build_handoff_continue_prompt(),
        delay_seconds=_terminal_clear._CODEX_CLEAR_CONTINUE_DELAY_SECONDS,
    )
    acknowledgment.assert_awaited_once()
    restore.assert_not_called()


async def test_codex_clear_stale_thread_end_banner_does_not_block_continuation() -> None:
    """A stale banner cannot suppress continuation after a successful /clear write."""
    session = _terminal_session()
    pane = _Pane(f"$ codex resume {_THREAD_ID}\n" + _THREAD_END_BANNER + _IDLE_PANE)
    restore = MagicMock(return_value=True)
    schedule = MagicMock(return_value=True)
    acknowledgment = AsyncMock(return_value=("successor-1", "successor_binding"))

    result = await _run_clear(
        _clear_patches(
            session,
            _clear_that_leaves_the_pane(),
            pane=pane,
            acknowledgment=acknowledgment,
            restore_failed_attempt=restore,
            schedule_continuation=schedule,
        ),
    )

    assert result["success"] is True
    assert result["acknowledged_by"] == "successor_binding"
    schedule.assert_called_once_with(
        session,
        build_handoff_continue_prompt(),
        delay_seconds=_terminal_clear._CODEX_CLEAR_CONTINUE_DELAY_SECONDS,
    )
    acknowledgment.assert_awaited_once()
    restore.assert_not_called()


async def test_codex_clear_counts_only_a_banner_printed_after_the_command() -> None:
    session = _terminal_session()
    pane = _Pane(f"$ codex resume {_THREAD_ID}\n" + _THREAD_END_BANNER + _IDLE_PANE)
    restore = MagicMock(return_value=True)
    schedule = MagicMock(return_value=True)

    result = await _run_clear(
        _clear_patches(
            session,
            _clear_that_ends_the_thread(pane),
            pane=pane,
            acknowledgment=AsyncMock(return_value=("successor-1", "successor_binding")),
            restore_failed_attempt=restore,
            schedule_continuation=schedule,
        ),
    )

    assert result["success"] is True
    schedule.assert_called_once()
    restore.assert_not_called()


async def test_codex_clear_without_thread_id_fails_closed_before_sending() -> None:
    session = _terminal_session(external_id=None)
    pane = _Pane(_IDLE_PANE)
    send_command = AsyncMock(return_value=(True, None, True, None))
    restore = MagicMock(return_value=True)
    schedule = MagicMock(return_value=True)

    result = await _run_clear(
        _clear_patches(
            session,
            send_command,
            pane=pane,
            acknowledgment=AsyncMock(),
            restore_failed_attempt=restore,
            schedule_continuation=schedule,
        ),
    )

    assert result["success"] is False
    assert result["error_code"] == "codex_thread_id_unavailable"
    assert result["command_sent"] is False
    assert result["attempt_restored"] is True
    send_command.assert_not_awaited()
    restore.assert_called_once()
    schedule.assert_not_called()


async def test_non_codex_clear_leaves_continuation_to_session_start() -> None:
    session = _terminal_session(source="claude", external_id="claude-session")
    pane = _Pane("> ")
    restore = MagicMock(return_value=True)
    schedule = MagicMock(return_value=True)
    acknowledgment = AsyncMock(return_value=("successor-2", "successor_binding"))

    result = await _run_clear(
        _clear_patches(
            session,
            _clear_that_leaves_the_pane(),
            pane=pane,
            acknowledgment=acknowledgment,
            restore_failed_attempt=restore,
            schedule_continuation=schedule,
        ),
    )

    assert result["success"] is True
    schedule.assert_not_called()
    restore.assert_not_called()
    acknowledgment.assert_awaited_once()


async def test_codex_clear_acknowledges_marker_consumed_by_typed_prompt() -> None:
    """The real acknowledgment wait binds the successor once the marker is consumed."""
    session = _terminal_session()
    pane = _Pane(_IDLE_PANE)
    stage_attempt = MagicMock(return_value=MagicMock())
    restore = MagicMock(return_value=True)
    schedule = MagicMock(return_value=True)

    class _VariablesAfterPrompt:
        """Marker reads as consumed only after the continuation prompt was scheduled."""

        def __init__(self, _db: Any) -> None:
            pass

        def get_variables(self, _session_id: str) -> dict[str, Any]:
            if not schedule.called:
                return {}
            return {
                CLEAR_ATTEMPT_VARIABLE: {
                    "attempt_id": stage_attempt.call_args.kwargs["attempt_id"],
                    "consumed_by": "successor-1",
                }
            }

    patches = _clear_patches(
        session,
        _clear_that_ends_the_thread(pane),
        pane=pane,
        acknowledgment=None,
        restore_failed_attempt=restore,
        schedule_continuation=schedule,
    )
    patches = [p for p in patches if p.attribute != "stage_clear_attempt"]
    patches.append(patch.object(_terminal_clear, "stage_clear_attempt", stage_attempt))
    patches.append(patch.object(_terminal_clear, "SessionVariableManager", _VariablesAfterPrompt))
    patches.append(patch.object(_terminal_clear, "_find_new_provider_session", return_value=None))

    result = await _run_clear(patches)

    assert result["success"] is True
    assert result["acknowledged_by"] == "successor_binding"
    assert result["successor_id"] == "successor-1"
    assert result["attempt_id"] == stage_attempt.call_args.kwargs["attempt_id"]
    schedule.assert_called_once()
    restore.assert_not_called()


def _pending_patches(
    session: SimpleNamespace,
    pane: _Pane,
    *,
    acknowledgment: Any,
    refresh: MagicMock,
    send_command: AsyncMock,
    stage_attempt: MagicMock,
) -> list[Any]:
    pending = {"attempt_id": "attempt-9", "command_sent_at": "2026-09-01T00:00:00+00:00"}
    patches = _base_patches(
        session, pane, restore_failed_attempt=MagicMock(return_value=True), pending=pending
    )
    patches = [p for p in patches if p.attribute != "stage_clear_attempt"]
    patches.extend(
        [
            patch.object(_terminal_clear, "stage_clear_attempt", stage_attempt),
            patch.object(_terminal_clear, "refresh_clear_attempt_content", refresh),
            patch.object(_terminal_clear, "_send_terminal_compaction_command", send_command),
            patch.object(_terminal_clear, "_wait_for_clear_acknowledgment", acknowledgment),
        ]
    )
    return patches


async def test_pending_attempt_is_reused_without_a_second_clear() -> None:
    session = _terminal_session(source="claude", external_id="claude-session")
    pane = _Pane("> ")
    refresh = MagicMock(return_value=True)
    send_command = AsyncMock()
    stage_attempt = MagicMock()

    result = await _run_clear(
        _pending_patches(
            session,
            pane,
            acknowledgment=AsyncMock(return_value=("successor-3", "successor_binding")),
            refresh=refresh,
            send_command=send_command,
            stage_attempt=stage_attempt,
        )
    )

    assert result["success"] is True
    assert result["reused_attempt"] is True
    assert result["content_refreshed"] is True
    assert result["attempt_id"] == "attempt-9"
    assert result["successor_id"] == "successor-3"
    assert pane.keys == []
    refresh.assert_called_once()
    assert refresh.call_args.kwargs == {
        "attempt_id": "attempt-9",
        "handoff": _HANDOFF,
    }
    assert refresh.call_args.args[1] == session.id
    send_command.assert_not_awaited()
    stage_attempt.assert_not_called()


async def test_pending_attempt_timeout_stays_pending() -> None:
    session = _terminal_session(source="claude", external_id="claude-session")
    send_command = AsyncMock()

    result = await _run_clear(
        _pending_patches(
            session,
            _Pane("> "),
            acknowledgment=AsyncMock(return_value=None),
            refresh=MagicMock(return_value=True),
            send_command=send_command,
            stage_attempt=MagicMock(),
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "clear_acknowledgment_timeout"
    assert result["reused_attempt"] is True
    assert result["attempt_pending"] is True
    assert result["attempt_restored"] is False
    assert result["attempt_id"] == "attempt-9"
    send_command.assert_not_awaited()

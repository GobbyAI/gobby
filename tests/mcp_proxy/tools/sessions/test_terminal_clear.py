from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.sessions import _terminal_clear


def _terminal_session() -> SimpleNamespace:
    return SimpleNamespace(
        id="session-1",
        status="active",
        source="codex",
        session_type="terminal",
        terminal_context={
            "tmux_pane": "%1",
            "tmux_socket_path": "/tmp/tmux-test",
        },
    )


@pytest.mark.asyncio
async def test_clear_delivery_survives_caller_cancellation() -> None:
    sender_started = asyncio.Event()
    release_sender = asyncio.Event()
    delivered_commands: list[str] = []

    async def send_command(
        _tmux: Any,
        _target: str,
        command: str,
        _session_id: str,
        **_kwargs: Any,
    ) -> tuple[bool, str | None, bool, dict[str, Any] | None]:
        sender_started.set()
        await release_sender.wait()
        delivered_commands.append(command)
        return True, None, True, None

    session = _terminal_session()
    tmux = MagicMock()
    tmux.capture_pane = AsyncMock(return_value="ready")
    agent_run_manager = MagicMock()
    agent_run_manager.get_by_session.return_value = None
    restore_failed_attempt = MagicMock(return_value=True)

    with (
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
        patch.object(
            _terminal_clear,
            "_resolve_tmux_target",
            return_value=("%1", tmux, None),
        ),
        patch.object(_terminal_clear, "_codex_interrupt_observer", return_value=lambda: True),
        patch.object(_terminal_clear, "stage_clear_attempt", return_value=MagicMock()),
        patch.object(_terminal_clear, "clear_failed_attempt", restore_failed_attempt),
        patch.object(_terminal_clear, "_send_terminal_compaction_command", send_command),
        patch.object(
            _terminal_clear,
            "_wait_for_clear_acknowledgment",
            new=AsyncMock(return_value=("successor-1", "successor_binding")),
        ),
    ):
        caller = asyncio.create_task(
            _terminal_clear.execute_clear_session(
                "## Current State\n\nReady to continue.",
                [],
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
    tmux = MagicMock()
    tmux.capture_pane = AsyncMock(return_value="ready")
    agent_run_manager = MagicMock()
    agent_run_manager.get_by_session.return_value = None
    restore_failed_attempt = MagicMock(return_value=True)
    stage_attempt = MagicMock(return_value=MagicMock())
    send_command = AsyncMock(return_value=(False, "delivery failed", False, None))

    with (
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
        patch.object(
            _terminal_clear,
            "_resolve_tmux_target",
            return_value=("%1", tmux, None),
        ),
        patch.object(_terminal_clear, "_codex_interrupt_observer", return_value=lambda: True),
        patch.object(_terminal_clear, "stage_clear_attempt", stage_attempt),
        patch.object(_terminal_clear, "clear_failed_attempt", restore_failed_attempt),
        patch.object(
            _terminal_clear,
            "_send_terminal_compaction_command",
            send_command,
        ),
    ):
        result = await _terminal_clear.execute_clear_session(
            "## Current State\n\nReady to continue.",
            [],
            session_manager=MagicMock(),
            db=MagicMock(),
            agent_run_manager=agent_run_manager,
        )

    assert result == {
        "success": False,
        "error": "delivery failed",
        "error_code": "clear_send_failed",
    }
    stage_attempt.assert_called_once()
    send_command.assert_awaited_once()
    await_args = send_command.await_args
    assert await_args is not None
    assert await_args.args[2] == "/clear"
    restore_failed_attempt.assert_called_once()

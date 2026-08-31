from __future__ import annotations

import asyncio
import os
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.sessions import _terminal_clear
from gobby.sessions.clear_continuation import CLEAR_ATTEMPT_VARIABLE
from gobby.sessions.handoff import build_handoff_continue_prompt


def _terminal_session(**overrides: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "id": "session-1",
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
            "_wait_for_new_codex_rollout",
            new=AsyncMock(return_value=Path("rollout-new.jsonl")),
        ),
        patch.object(_terminal_clear, "schedule_handoff_continuation", return_value=True),
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


def _clear_patches(
    session: SimpleNamespace,
    send_command: Any,
    *,
    acknowledgment: Any,
    restore_failed_attempt: MagicMock,
    schedule_continuation: MagicMock,
) -> list[Any]:
    tmux = MagicMock()
    tmux.capture_pane = AsyncMock(return_value="ready")
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
        patch.object(_terminal_clear, "_resolve_tmux_target", return_value=("%1", tmux, None)),
        patch.object(_terminal_clear, "_codex_interrupt_observer", return_value=lambda: True),
        patch.object(_terminal_clear, "stage_clear_attempt", return_value=MagicMock()),
        patch.object(_terminal_clear, "clear_failed_attempt", restore_failed_attempt),
        patch.object(_terminal_clear, "_send_terminal_compaction_command", send_command),
        patch.object(_terminal_clear, "schedule_handoff_continuation", schedule_continuation),
        patch.object(_terminal_clear, "_CODEX_CLEAR_ROLLOUT_TIMEOUT_SECONDS", 0.2),
        patch.object(_terminal_clear, "_CODEX_CLEAR_ROLLOUT_POLL_SECONDS", 0.01),
    ] + (
        []
        if acknowledgment is None
        else [patch.object(_terminal_clear, "_wait_for_clear_acknowledgment", acknowledgment)]
    )


async def _run_clear(session: SimpleNamespace, patches: list[Any]) -> dict[str, Any]:
    agent_run_manager = MagicMock()
    agent_run_manager.get_by_session.return_value = None
    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        return await _terminal_clear.execute_clear_session(
            "## Current State\n\nReady to continue.",
            [],
            session_manager=MagicMock(),
            db=MagicMock(),
            agent_run_manager=agent_run_manager,
        )


def _rollout_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "sessions" / "2026" / "08" / "30"
    directory.mkdir(parents=True)
    return directory


async def test_codex_clear_types_continuation_once_new_rollout_appears(tmp_path: Path) -> None:
    rollouts = _rollout_dir(tmp_path)
    predecessor = rollouts / "rollout-old.jsonl"
    predecessor.write_text("{}\n")
    session = _terminal_session(transcript_path=str(predecessor))

    async def send_command(*_args: Any, **_kwargs: Any) -> tuple[bool, None, bool, None]:
        (rollouts / "rollout-new.jsonl").write_text("{}\n")
        return True, None, True, None

    restore = MagicMock(return_value=True)
    schedule = MagicMock(return_value=True)
    acknowledgment = AsyncMock(return_value=("successor-1", "successor_binding"))

    result = await _run_clear(
        session,
        _clear_patches(
            session,
            send_command,
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
    restore.assert_not_called()


async def test_codex_clear_without_new_rollout_restores_attempt(tmp_path: Path) -> None:
    rollouts = _rollout_dir(tmp_path)
    predecessor = rollouts / "rollout-old.jsonl"
    predecessor.write_text("{}\n")
    session = _terminal_session(transcript_path=str(predecessor))

    async def send_command(*_args: Any, **_kwargs: Any) -> tuple[bool, None, bool, None]:
        return True, None, True, None

    restore = MagicMock(return_value=True)
    schedule = MagicMock(return_value=True)
    acknowledgment = AsyncMock(return_value=("successor-1", "successor_binding"))

    result = await _run_clear(
        session,
        _clear_patches(
            session,
            send_command,
            acknowledgment=acknowledgment,
            restore_failed_attempt=restore,
            schedule_continuation=schedule,
        ),
    )

    assert result["success"] is False
    assert result["error_code"] == "clear_successor_not_observed"
    assert result["command_sent"] is True
    assert result["attempt_restored"] is True
    restore.assert_called_once()
    schedule.assert_not_called()
    acknowledgment.assert_not_awaited()


async def test_non_codex_clear_leaves_continuation_to_session_start(tmp_path: Path) -> None:
    session = _terminal_session(source="claude", transcript_path=str(tmp_path / "missing.jsonl"))

    async def send_command(*_args: Any, **_kwargs: Any) -> tuple[bool, None, bool, None]:
        return True, None, True, None

    restore = MagicMock(return_value=True)
    schedule = MagicMock(return_value=True)
    acknowledgment = AsyncMock(return_value=("successor-2", "successor_binding"))

    result = await _run_clear(
        session,
        _clear_patches(
            session,
            send_command,
            acknowledgment=acknowledgment,
            restore_failed_attempt=restore,
            schedule_continuation=schedule,
        ),
    )

    assert result["success"] is True
    schedule.assert_not_called()
    restore.assert_not_called()
    acknowledgment.assert_awaited_once()


def test_find_new_codex_rollout_ignores_predecessor_and_older_files(tmp_path: Path) -> None:
    rollouts = _rollout_dir(tmp_path)
    predecessor = rollouts / "rollout-old.jsonl"
    predecessor.write_text("{}\n")
    stale = rollouts / "rollout-stale.jsonl"
    stale.write_text("{}\n")
    stale_mtime = time.time() - 600
    os.utime(stale, (stale_mtime, stale_mtime))
    since = time.time()

    assert _terminal_clear._find_new_codex_rollout(str(predecessor), since=since) is None

    fresh = rollouts / "rollout-fresh.jsonl"
    fresh.write_text("{}\n")

    assert _terminal_clear._find_new_codex_rollout(str(predecessor), since=since) == fresh
    assert _terminal_clear._find_new_codex_rollout(None, since=since) is None


async def test_codex_clear_acknowledges_marker_consumed_by_typed_prompt(tmp_path: Path) -> None:
    """The real acknowledgment wait binds the successor once the marker is consumed."""
    rollouts = _rollout_dir(tmp_path)
    predecessor = rollouts / "rollout-old.jsonl"
    predecessor.write_text("{}\n")
    session = _terminal_session(transcript_path=str(predecessor))

    async def send_command(*_args: Any, **_kwargs: Any) -> tuple[bool, None, bool, None]:
        (rollouts / "rollout-new.jsonl").write_text("{}\n")
        return True, None, True, None

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
        send_command,
        acknowledgment=None,
        restore_failed_attempt=restore,
        schedule_continuation=schedule,
    )
    patches = [p for p in patches if getattr(p, "attribute", None) != "stage_clear_attempt"]
    patches.append(patch.object(_terminal_clear, "stage_clear_attempt", stage_attempt))
    patches.append(patch.object(_terminal_clear, "SessionVariableManager", _VariablesAfterPrompt))
    patches.append(patch.object(_terminal_clear, "_find_new_provider_session", return_value=None))

    result = await _run_clear(session, patches)

    assert result["success"] is True
    assert result["acknowledged_by"] == "successor_binding"
    assert result["successor_id"] == "successor-1"
    assert result["attempt_id"] == stage_attempt.call_args.kwargs["attempt_id"]
    schedule.assert_called_once()
    restore.assert_not_called()

"""Focused tests for terminal prompt callback isolation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.terminal_prompt_monitor import TerminalPromptMonitor
from gobby.agents.tmux.text_injection import TmuxTargetUnavailableError
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.runtime import TerminalWriteError
from gobby.terminals.services import TerminalServices
from gobby.terminals.write_coordinator import WriteCoordinator
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit


def _run() -> AgentRun:
    return AgentRun(
        id="run-1",
        parent_session_id="parent-1",
        provider="claude",
        prompt="test",
        status="running",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        terminal_id="agent-run-1",
    )


@pytest.mark.asyncio
async def test_prompt_callback_failure_preserves_successful_injection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = _run()
    detector = MagicMock()
    detector.was_dismissed.return_value = False
    detector.detect_trust_prompt.return_value = True
    prompt_detector = MagicMock()
    prompt_detector.for_provider.return_value = detector
    terminal = make_memory_terminal(terminal_id="agent-run-1", session_name="agent-run-1")
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime()
    runtime.snapshot_text = "trust prompt"
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    services = TerminalServices(
        manager=store,
        registry=registry,
        coordinator=WriteCoordinator(store, runtime_registry(runtime)),
    )
    callback = AsyncMock(side_effect=RuntimeError("callback failed"))
    monitor = TerminalPromptMonitor(
        get_active_terminal_runs=lambda: [run],
        get_tmux=lambda: MagicMock(),
        prompt_detector=prompt_detector,
        loop_tracker=MagicMock(),
        get_tmux_config=TmuxConfig,
        handle_looping_agent=AsyncMock(),
        on_prompt_injected=callback,
        terminal_services=services,
    )

    handled = await monitor.check_trust_prompts()

    assert handled == 1
    detector.mark_dismissed.assert_called_once_with(run.id)
    callback.assert_awaited_once_with(run)
    assert "Prompt-injection callback failed for agent run-1" in caplog.text


def _monitor_with_probe_error(error: Exception) -> TerminalPromptMonitor:
    """Build a monitor whose first pane probe raises `error`."""
    terminal = make_memory_terminal(terminal_id="agent-run-1", session_name="agent-run-1")
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime()
    runtime.snapshot_effects = [error]
    services = TerminalServices(
        manager=store,
        registry=runtime_registry(runtime),
        coordinator=WriteCoordinator(store, runtime_registry(runtime)),
    )
    detector = MagicMock()
    detector.was_dismissed.return_value = False
    prompt_detector = MagicMock()
    prompt_detector.for_provider.return_value = detector
    return TerminalPromptMonitor(
        get_active_terminal_runs=lambda: [_run()],
        get_tmux=lambda: MagicMock(),
        prompt_detector=prompt_detector,
        loop_tracker=MagicMock(),
        get_tmux_config=TmuxConfig,
        handle_looping_agent=AsyncMock(),
        on_prompt_injected=AsyncMock(),
        terminal_services=services,
    )


@pytest.mark.asyncio
async def test_chained_write_error_logs_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """The vanished-target marker lives in __cause__; the probe must stay quiet."""
    cause = TmuxTargetUnavailableError(
        "tmux target is unavailable: can't find session: agent-run-1",
        command=("tmux", "capture-pane"),
        stderr="can't find session: agent-run-1",
        returncode=1,
    )
    try:
        raise TerminalWriteError(stage="none") from cause
    except TerminalWriteError as chained:
        monitor = _monitor_with_probe_error(chained)

    with caplog.at_level(logging.DEBUG, logger="gobby.agents.terminal_prompt_monitor"):
        handled = await monitor.check_trust_prompts()

    assert handled == 0
    assert "Prompt probe trust skipped" in caplog.text
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


@pytest.mark.asyncio
async def test_unexpected_probe_error_still_warns_with_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A misrouted write is a defect, not a race, and must keep its traceback."""
    monitor = _monitor_with_probe_error(RuntimeError("terminal store exploded"))

    with caplog.at_level(logging.DEBUG, logger="gobby.agents.terminal_prompt_monitor"):
        handled = await monitor.check_trust_prompts()

    assert handled == 0
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Prompt probe trust failed" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None

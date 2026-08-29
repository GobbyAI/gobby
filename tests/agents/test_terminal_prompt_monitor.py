"""Focused tests for terminal prompt callback isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.terminal_prompt_monitor import TerminalPromptMonitor
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun
from gobby.terminals import TerminalRuntimeRegistry
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

"""Watchdogs leave parked and mid-turn agents alone (#20713).

Two quiet-but-alive shapes used to read as idle/stagnant:

- a run whose session called ``wait_for_agent`` and is parked until the daemon
  wakes it with the subscribed completion;
- a run whose provider is mid-turn in a long thinking phase, which writes nothing
  to the transcript and emits no hook events while the pane spinner ticks.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.agents.idle_detector import IdleDetector
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.config.tmux import TmuxConfig
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.agents.test_lifecycle_monitor import (
    DETECTION_REGISTRY,
    TerminalWakeRecorder,
    _make_progress_stagnation_monitor,
    _make_terminal_run,
    _rid,
)

pytestmark = pytest.mark.unit

THINKING_PANE = "✻ Grooving… (7m 12s · still thinking with xhigh effort)\n❯ \n"
THINKING_PANE_LATER = "✻ Grooving… (7m 42s · still thinking with xhigh effort)\n❯ \n"
CODEX_WORKING_PANE = "• Working (4m 58s • esc to interrupt)\n\n› Ask Codex to do anything\n"
IDLE_PANE = "Ran tests… done.\n❯ \n"


# --- completion registry -----------------------------------------------------


async def test_registry_reports_awaiting_until_notified() -> None:
    registry = CompletionEventRegistry()
    registry.register("child-run", subscribers=["waiting-session"])

    assert registry.is_awaiting("waiting-session") is True
    assert registry.is_awaiting("other-session") is False

    await registry.notify("child-run", {"status": "success"})
    assert registry.is_awaiting("waiting-session") is False

    registry.cleanup("child-run")
    assert registry.is_awaiting("waiting-session") is False


# --- idle detector ------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "pane"),
    [("claude", THINKING_PANE), ("codex", CODEX_WORKING_PANE)],
)
def test_turn_in_flight_is_detected_above_a_visible_prompt(provider: str, pane: str) -> None:
    detector = IdleDetector(BundledDetectionRegistry(), provider)

    assert detector.has_turn_in_flight(pane) is True
    assert detector.has_turn_in_flight(IDLE_PANE) is False


def test_turn_in_flight_fingerprint_follows_the_elapsed_counter() -> None:
    detector = IdleDetector(BundledDetectionRegistry(), "claude")

    first = detector.turn_in_flight_fingerprint(THINKING_PANE)
    later = detector.turn_in_flight_fingerprint(THINKING_PANE_LATER)
    assert first is not None and later is not None
    assert first != later
    assert detector.turn_in_flight_fingerprint(THINKING_PANE) == first


def test_plain_prompt_still_reads_idle() -> None:
    detector = IdleDetector(BundledDetectionRegistry(), "claude")

    assert detector.detect(IDLE_PANE) == "idle"


# --- idle watchdog ------------------------------------------------------------


def _idle_monitor(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    registry: CompletionEventRegistry | None = None,
) -> AgentLifecycleMonitor:
    return AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        completion_registry=registry,
        tmux_config=TmuxConfig(
            idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2
        ),
    )


async def test_parked_agent_is_never_reprompted_until_its_wait_resolves(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict[str, Any],
) -> None:
    registry = CompletionEventRegistry()
    monitor = _idle_monitor(agent_run_manager, temp_db, registry)
    run = _make_terminal_run(
        agent_run_manager,
        sample_session,
        run_id=_rid("run-parked"),
        terminal_id="gobby-parked",
        child_session_id=sample_session["id"],
    )
    registry.register(_rid("validator-run"), subscribers=[sample_session["id"]])
    monitor._idle_detector.get_state(run.id).first_idle_at = time.monotonic() - 360

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(monitor._tmux, "send_keys", new=TerminalWakeRecorder()) as wake,
    ):
        parked = await monitor.check_idle_agents()
        await registry.notify(_rid("validator-run"), {"status": "success"})
        monitor._idle_detector.get_state(run.id).first_idle_at = time.monotonic() - 360
        resolved = await monitor.check_idle_agents()

    assert parked == 0
    assert resolved == 1
    assert [keys for _session, keys, _literal in wake.calls][0] == "Escape"


async def test_agent_mid_turn_is_not_reprompted(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict[str, Any],
) -> None:
    monitor = _idle_monitor(agent_run_manager, temp_db)
    run = _make_terminal_run(
        agent_run_manager,
        sample_session,
        run_id=_rid("run-thinking"),
        terminal_id="gobby-thinking",
    )
    monitor._idle_detector.get_state(run.id).first_idle_at = time.monotonic() - 360

    with (
        patch.object(
            monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value=THINKING_PANE
        ),
        patch.object(monitor._tmux, "send_keys", new=TerminalWakeRecorder()) as wake,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 0
    assert wake.calls == []
    assert monitor._idle_detector.get_state(run.id).first_idle_at is None


# --- stuck watchdog (progress stagnation) -----------------------------------


async def test_parked_agent_is_not_stagnant_until_its_wait_resolves(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict[str, Any],
) -> None:
    registry = CompletionEventRegistry()
    monitor, run, _detector = _make_progress_stagnation_monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        sample_session=sample_session,
    )
    monitor._completion_registry = registry
    # The stuck check keys on the run's session (parent here); that session waits.
    registry.register(_rid("validator-run-2"), subscribers=[sample_session["id"]])

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._cleanup_handler, "cleanup_agent", new_callable=AsyncMock
        ) as cleanup_agent,
    ):
        parked = await monitor.check_autonomous_stuck_agents()
        await registry.notify(_rid("validator-run-2"), {"status": "success"})
        resolved = await monitor.check_autonomous_stuck_agents()

    assert parked == 0
    assert resolved == 1
    cleanup_agent.assert_awaited_once()


async def test_live_spinner_defers_stagnation_and_a_frozen_one_does_not(
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    sample_session: dict[str, Any],
) -> None:
    monitor, run, _detector = _make_progress_stagnation_monitor(
        agent_run_manager=agent_run_manager,
        temp_db=temp_db,
        sample_session=sample_session,
    )
    panes = iter([THINKING_PANE, THINKING_PANE_LATER, THINKING_PANE_LATER])

    async def capture_pane(_name: str, lines: int = 15) -> str:
        return next(panes)

    with (
        patch.object(monitor._tmux, "capture_pane", new=capture_pane),
        patch.object(
            monitor._cleanup_handler, "cleanup_agent", new_callable=AsyncMock
        ) as cleanup_agent,
    ):
        first = await monitor.check_autonomous_stuck_agents()
        ticking = await monitor.check_autonomous_stuck_agents()
        fingerprint, _seen = monitor._draft_grace_observations[run.id]
        monitor._draft_grace_observations[run.id] = (fingerprint, time.monotonic() - 3600)
        frozen = await monitor.check_autonomous_stuck_agents()

    assert [first, ticking, frozen] == [0, 0, 1]
    cleanup_agent.assert_awaited_once_with(
        run,
        terminal_payload="autonomous stuck: No progress events for 634 seconds",
    )

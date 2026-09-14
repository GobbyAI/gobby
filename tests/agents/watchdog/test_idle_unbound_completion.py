"""Responsive spawned runs with no step workflow or task complete once reprompts run out (#22349).

A workflow-less, taskless spawned agent gives completed-turn reprompts no
lifecycle obligation to drive. Reprompts steer it to end_agent_run; if it keeps
completing turns without calling it, the watchdog completes the run instead of
reporting finished work as an error. A run holding a claimed task keeps failing
(test_completed_turn_recovery_allows_budget_then_fails_run_holding_claimed_task).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from gobby.agents.idle_detector import IdleDetector
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.agents.test_lifecycle_monitor import _pane_text, _runtime_of
from tests.agents.test_lifecycle_monitor_watchdog_idle_recovery import (
    _make_idle_monitor_run,
    _write_codex_lifecycle_transcript,
)

pytestmark = pytest.mark.unit

_EXHAUSTED_ERROR = "completed another turn without workflow progress"


@pytest.mark.asyncio
async def test_completed_turn_exhaustion_completes_unbound_run(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript_path = tmp_path / "codex-unbound.jsonl"
    _write_codex_lifecycle_transcript(transcript_path, age_seconds=120)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd2234",
        transcript_path=transcript_path,
        max_reprompt_attempts=3,
    )
    caplog.set_level(logging.INFO)

    with _pane_text(monitor, "❯\n"):
        for attempt in range(4):
            _write_codex_lifecycle_transcript(transcript_path, age_seconds=120 + attempt)
            monitor._idle_detector.reset_idle(run.id)
            assert await monitor.check_idle_agents() == 1

    reprompts = [text for kind, text in _runtime_of(monitor).write_log if kind == "text"]
    assert reprompts == [IdleDetector.REPROMPT_MESSAGE] * 3
    assert "gobby-agents:end_agent_run" in reprompts[0]
    updated_run = agent_run_manager.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "success"
    assert any("no step workflow or task" in r.getMessage() for r in caplog.records)
    assert not any(_EXHAUSTED_ERROR in r.getMessage() for r in caplog.records)

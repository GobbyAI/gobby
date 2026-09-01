"""Idle task agents whose task is closed or handed back complete instead of failing (#21516).

An implementer that escalates its task back to the coordinator (or closes it)
and then idles has no work left: failing it there reports finished work as an
error, exactly the #19097 misreport for workflow-less task agents.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from tests.agents.test_lifecycle_monitor import _pane_text
from tests.agents.test_lifecycle_monitor_watchdog_idle_recovery import (
    _make_idle_monitor_run,
    _write_codex_lifecycle_transcript,
)

pytestmark = pytest.mark.unit

_EXHAUSTED_ERROR = "completed another turn without workflow progress"


def _escalate(task_manager: LocalTaskManager, task_id: str) -> None:
    task_manager.escalate_task(task_id, reason="handback: QA and land")


def _close(task_manager: LocalTaskManager, task_id: str) -> None:
    task_manager.close_task(task_id, reason="done")


def _keep_claimed(task_manager: LocalTaskManager, task_id: str) -> None:
    del task_manager, task_id


def _task_bound_run(
    *,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    run_id: str,
    transcript_path: Path | None,
) -> tuple[Any, Any, LocalTaskManager]:
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Implement and hand back",
        validation_criteria="Handback completion is observable.",
    )
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id=run_id,
        transcript_path=transcript_path,
        task_manager=task_manager,
        max_reprompt_attempts=3,
        task_id=task.id,
    )
    assert run.child_session_id is not None
    task_manager.claim_task(task.id, run.child_session_id)
    return monitor, run, task_manager


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transition", "expected_status", "expected_log"),
    [
        pytest.param(_escalate, "success", "handed back", id="escalated"),
        pytest.param(_close, "success", "closed", id="closed"),
        pytest.param(_keep_claimed, "error", _EXHAUSTED_ERROR, id="still-claimed"),
    ],
)
async def test_exhausted_completed_turn_recovery_follows_task_state(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    transition: Callable[[LocalTaskManager, str], None],
    expected_status: str,
    expected_log: str,
) -> None:
    transcript_path = tmp_path / "codex-handback.jsonl"
    _write_codex_lifecycle_transcript(transcript_path, age_seconds=120)
    monitor, run, task_manager = _task_bound_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1516",
        transcript_path=transcript_path,
    )
    assert run.task_id is not None
    transition(task_manager, run.task_id)
    caplog.set_level(logging.INFO)

    with _pane_text(monitor, "❯\n"):
        for attempt in range(3):
            _write_codex_lifecycle_transcript(transcript_path, age_seconds=120 + attempt)
            monitor._idle_detector.reset_idle(run.id)
            assert await monitor.check_idle_agents() == 1

        _write_codex_lifecycle_transcript(transcript_path, age_seconds=123)
        monitor._idle_detector.reset_idle(run.id)
        assert await monitor.check_idle_agents() == 1

    updated_run = agent_run_manager.get(run.id)
    assert updated_run is not None
    assert updated_run.status == expected_status
    assert any(expected_log in record.getMessage() for record in caplog.records)
    if expected_status == "success":
        assert not any(_EXHAUSTED_ERROR in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_max_idle_reprompts_complete_run_whose_task_was_handed_back(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The transcript-less idle ladder shares the completion check."""
    monitor, run, task_manager = _task_bound_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1517",
        transcript_path=None,
    )
    assert run.task_id is not None
    _escalate(task_manager, run.task_id)
    monitor._idle_detector.get_state(run.id).reprompt_count = 3
    caplog.set_level(logging.INFO)

    with _pane_text(monitor, "❯\n"):
        assert await monitor.check_idle_agents() == 1

    updated_run = agent_run_manager.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "success"
    assert not any("idle after max reprompt attempts" in r.getMessage() for r in caplog.records)
    assert any("handed back" in record.getMessage() for record in caplog.records)

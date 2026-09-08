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
from unittest.mock import AsyncMock, patch

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
async def test_completed_turn_recovery_follows_task_state_before_reprompt(
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
        attempts = 1 if expected_status == "success" else 3
        for attempt in range(attempts):
            _write_codex_lifecycle_transcript(transcript_path, age_seconds=120 + attempt)
            monitor._idle_detector.reset_idle(run.id)
            assert await monitor.check_idle_agents() == 1

        if expected_status == "error":
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


@pytest.mark.asyncio
async def test_watchdog_completion_persists_final_closed_task_details(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
) -> None:
    monitor, run, task_manager = _task_bound_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1518",
        transcript_path=None,
    )
    assert run.task_id is not None
    task_manager.close_task(run.task_id, reason="done", closed_commit_sha="abc123")
    closed_task = task_manager.get_task(run.task_id)
    assert closed_task.closed_at is not None
    recovery = monitor._idle_check_handler._recovery

    with (
        patch.object(recovery._terminal_services, "terminal_for", return_value=None),
        patch.object(monitor._cleanup_handler, "post_terminal_cleanup", new_callable=AsyncMock),
    ):
        handled = await recovery._complete_if_work_finished(run)

    completed = agent_run_manager.get(run.id)
    suffix = (
        "Task completion: "
        f"task=#{closed_task.seq_num}; closed_at={closed_task.closed_at.isoformat()}; "
        "commit_sha=abc123"
    )
    assert handled is True
    assert completed is not None
    assert completed.status == "success"
    assert completed.error is None
    assert completed.result == (
        f"Agent completed by watchdog: task {run.task_id} was closed "
        f"but the agent never called end_agent_run\n\n{suffix}"
    )


@pytest.mark.asyncio
async def test_watchdog_blocker_completion_uses_structured_terminalizer(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
) -> None:
    monitor, run, _task_manager = _task_bound_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1519",
        transcript_path=None,
    )
    assert run.child_session_id is not None
    from gobby.workflows.state_manager import SessionVariableManager

    SessionVariableManager(temp_db).merge_variables(
        run.child_session_id,
        {"blocker_handed_off": True},
    )
    recovery = monitor._idle_check_handler._recovery

    with (
        patch(
            "gobby.agents.watchdog.recovery.agent_run_task_dirty_paths",
            return_value=["src/dirty.py"],
        ),
        patch.object(
            monitor._cleanup_handler,
            "terminalize_successful_run",
            new_callable=AsyncMock,
            return_value=True,
        ) as terminalize,
    ):
        await recovery._complete_idle_agent(run, "workflow reached terminate")

    terminalize.assert_awaited_once_with(
        run.id,
        notify_result={
            "status": "blocked",
            "run_id": run.id,
            "dirty_paths": ["src/dirty.py"],
            "terminal_reason": "task_blocker",
        },
        message=f'Agent {run.id} completed; dirty_paths=["src/dirty.py"]',
        terminal_reason="task_blocker",
    )

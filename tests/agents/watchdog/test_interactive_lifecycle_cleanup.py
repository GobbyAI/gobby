"""Standing seats retain process cleanup and provider-error handling."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from tests.agents.test_lifecycle_monitor import _pane_text, _runtime_of
from tests.agents.test_lifecycle_monitor_watchdog_idle_recovery import (
    _CAPACITY_PANE,
    _append_codex_capacity_turn,
    _make_idle_monitor_run,
    _write_codex_lifecycle_transcript,
)
from tests.test_runner_lifecycle_restart_replay import (
    TestAgentRestartReconciliation as _RestartFixtures,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("task_state", ["closed", "close_handoff"])
async def test_dead_interactive_seat_is_cleaned_up_after_task_close(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    task_state: str,
) -> None:
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id=sample_project["id"],
        title="Seat's finished task",
        validation_criteria="A dead standing seat is cleaned up after task closure.",
    )
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="eeeeeeee-eeee-4eee-8eee-eeeeeeee2342",
        transcript_path=None,
        task_manager=tasks,
        task_id=task.id,
    )
    agent_run_manager.update_resume_metadata(run.id, {"execution_mode": "interactive"})
    if task_state == "closed":
        tasks.close_task(task.id)
    _runtime_of(monitor).alive = False

    with patch(
        "gobby.agents.agent_health.cooperative_close_handoff_pending",
        return_value=task_state == "close_handoff",
    ):
        assert await monitor.check_completed_task_agents() == 0
        assert await monitor.check_unhealthy_agents() == 1

    stored = agent_run_manager.get(run.id)
    assert stored is not None
    assert stored.status == "error"
    assert "terminal session died unexpectedly" in (stored.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("is_interactive", [False, True])
@pytest.mark.parametrize("task_state", ["closed", "close_handoff", "ended_review"])
async def test_missing_interactive_terminal_resumes_after_task_close(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    task_state: str,
    is_interactive: bool,
) -> None:
    from gobby.runner_lifecycle_reconcile import _cleanup_missing_terminal_agent_run

    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id=sample_project["id"],
        title="Seat's finished task",
        validation_criteria="A missing standing seat is parked and resumed after task closure.",
    )
    _monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="eeeeeeee-eeee-4eee-8eee-eeeeeeee2343",
        transcript_path=None,
        task_manager=tasks,
        task_id=task.id,
    )
    mode = "interactive" if is_interactive else "one_shot"
    metadata: dict[str, Any] = {"execution_mode": mode}
    if is_interactive:
        metadata["idle_ttl_seconds"] = 900
    stored = agent_run_manager.update_resume_metadata(run.id, metadata)
    assert stored is not None
    if task_state == "closed":
        tasks.close_task(task.id)
    runner = _RestartFixtures()._runner(agent_run_manager, db=temp_db)

    with (
        patch(
            "gobby.agents.run_completion.cooperative_close_handoff_pending",
            return_value=task_state == "close_handoff",
        ),
        patch(
            "gobby.agents.run_completion.ended_caller_close_review_outcome",
            return_value=task_state == "ended_review",
        ),
        patch(
            "gobby.agents.resume_executor.resume_agent_run",
            new=AsyncMock(return_value=SimpleNamespace(success=True)),
        ) as resume,
    ):
        assert await _cleanup_missing_terminal_agent_run(runner, stored, "missing-seat") is True

    if is_interactive:
        runner.agent_lifecycle_monitor.terminalize_cancelled_run.assert_awaited_once_with(
            run.id, terminal_reason="daemon_stop"
        )
        resume.assert_awaited_once()
        assert resume.call_args.kwargs["resume_metadata"]["execution_mode"] == "interactive"
        assert resume.call_args.kwargs["resume_metadata"]["idle_ttl_seconds"] == 900
    else:
        runner.agent_lifecycle_monitor.terminalize_cancelled_run.assert_not_awaited()
        resume.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_problem", ["quota", "context_full", "terminal_error", "capacity"]
)
async def test_interactive_seat_still_handles_provider_failures(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
    provider_problem: str,
) -> None:
    transcript_path = tmp_path / "seat-provider-error.jsonl"
    pane = "❯\n"
    if provider_problem == "capacity":
        _append_codex_capacity_turn(transcript_path)
        pane = _CAPACITY_PANE
    else:
        _write_codex_lifecycle_transcript(transcript_path, age_seconds=1200)
        if provider_problem == "quota":
            pane = "You've hit your usage limit.\n❯\n"
        elif provider_problem == "context_full":
            pane = "The context window is full.\n❯\n"
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="eeeeeeee-eeee-4eee-8eee-eeeeeeee2344",
        transcript_path=transcript_path,
    )
    agent_run_manager.update_resume_metadata(run.id, {"execution_mode": "interactive"})

    if provider_problem in {"quota", "terminal_error"}:
        error_info = (
            "usage_limit_exceeded" if provider_problem == "quota" else "internal_server_error"
        )
        _write_codex_lifecycle_transcript(
            transcript_path,
            event_timestamp=datetime.now(UTC),
            task_complete_error={"message": "Provider failure.", "codex_error_info": error_info},
        )

    with _pane_text(monitor, pane):
        assert await monitor.check_idle_agents() == 1

    stored = agent_run_manager.get(run.id)
    assert stored is not None
    if provider_problem == "capacity":
        assert stored.status == "running"
        state = monitor._idle_check_handler._recovery._capacity_recovery[run.id]
        assert state.successful_reprompts == 1
        reprompts = [text for kind, text in _runtime_of(monitor).write_log if kind == "text"]
        assert len(reprompts) == 1
        assert "end_agent_run" not in reprompts[0]
    else:
        assert stored.status == "error"
        assert stored.error
        if provider_problem == "terminal_error":
            assert stored.terminal_reason == "provider_error"
        elif provider_problem == "quota":
            assert stored.terminal_reason == "provider_quota_exhausted"
        else:
            assert "context window exhausted" in stored.error

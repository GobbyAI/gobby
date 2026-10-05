"""Standing spawned seats may wait at the prompt without ending their run."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.agents.idle_detector import IdleDetector
from gobby.autonomous.stuck_detector import StuckDetectionResult
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, TaskDispatchMutexManager
from gobby.workflows.engine import RuleEngine
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.agents.test_lifecycle_monitor import _pane_text, _runtime_of
from tests.agents.test_lifecycle_monitor_watchdog_idle_recovery import (
    _make_idle_monitor_run,
    _write_codex_lifecycle_transcript,
)
from tests.agents.watchdog.test_completed_turn_mcp_gate import _snapshot
from tests.workflows.step_instance_fixtures import make_step_instance

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("has_transcript", [True, False])
@pytest.mark.parametrize("made_mcp_call", [True, False])
async def test_interactive_spawned_seat_idle_is_not_reprompted_or_completed(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
    has_transcript: bool,
    made_mcp_call: bool,
) -> None:
    transcript_path = tmp_path / "standing-seat.jsonl" if has_transcript else None
    if transcript_path is not None:
        _write_codex_lifecycle_transcript(transcript_path, age_seconds=1200)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd2342",
        transcript_path=transcript_path,
        session_age_seconds=1200,
        max_reprompt_attempts=3,
        made_gobby_mcp_call=made_mcp_call,
    )
    agent_run_manager.update_resume_metadata(run.id, {"execution_mode": "interactive"})

    with _pane_text(monitor, "❯\n"):
        for attempt in range(5):
            if transcript_path is not None:
                _write_codex_lifecycle_transcript(transcript_path, age_seconds=1200 + attempt)
            state = monitor._idle_detector.get_state(run.id)
            state.first_idle_at = 0
            assert await monitor.check_idle_agents() == 0

    assert _runtime_of(monitor).write_log == []
    stored = agent_run_manager.get(run.id)
    assert stored is not None
    assert stored.status == "running"
    assert stored.completed_at is None


@pytest.mark.asyncio
async def test_one_shot_unbound_run_still_completed_after_max_reprompts(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "one-shot.jsonl"
    _write_codex_lifecycle_transcript(transcript_path, age_seconds=120)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd2343",
        transcript_path=transcript_path,
        max_reprompt_attempts=3,
    )
    agent_run_manager.update_resume_metadata(run.id, {"execution_mode": "one_shot"})

    with _pane_text(monitor, "❯\n"):
        for attempt in range(4):
            _write_codex_lifecycle_transcript(transcript_path, age_seconds=120 + attempt)
            monitor._idle_detector.reset_idle(run.id)
            assert await monitor.check_idle_agents() == 1

    reprompts = [text for kind, text in _runtime_of(monitor).write_log if kind == "text"]
    assert reprompts == [IdleDetector.REPROMPT_MESSAGE] * 3
    stored = agent_run_manager.get(run.id)
    assert stored is not None
    assert stored.status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["stuck", "task_closed", "workflow_finished", "completed_turn"])
async def test_interactive_seat_survives_automatic_lifecycle_transitions(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    path: str,
) -> None:
    tasks = LocalTaskManager(temp_db)
    task = tasks.create_task(
        project_id=sample_project["id"],
        title="Seat's current task",
        validation_criteria="The seat remains running after the task ends.",
    )
    stuck_detector = MagicMock()
    stuck_detector.is_stuck.return_value = StuckDetectionResult(
        is_stuck=True, reason="no progress", layer="stagnation", suggested_action="stop"
    )
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd2344",
        transcript_path=None,
        task_manager=tasks,
        task_id=task.id,
        stuck_detector=stuck_detector,
    )
    interactive = agent_run_manager.update_resume_metadata(
        run.id, {"execution_mode": "interactive"}
    )
    assert interactive is not None
    assert run.child_session_id is not None
    with _pane_text(monitor, "❯\n"):
        if path == "stuck":
            assert await monitor.check_autonomous_stuck_agents() == 0
        elif path == "task_closed":
            tasks.close_task(task.id)
            assert await monitor.check_completed_task_agents() == 0
        elif path == "workflow_finished":
            instances = AgentStepInstanceManager(temp_db)
            instances.save(make_step_instance(run.child_session_id, agent_name="default"))
            mutexes = TaskDispatchMutexManager(temp_db)
            mutexes.acquire_mutex(
                task.id, holder="dispatcher", kind="spawn_agent", run_id=run.id, ttl_seconds=300
            )
            runner = MagicMock(run_storage=agent_run_manager, agent_lifecycle_monitor=monitor)
            engine = RuleEngine(db=temp_db, runner=runner)
            await engine._complete_agent_workflow_run(run.child_session_id, "seat-runbook", {})
            assert instances.get_for_session(run.child_session_id) is None
            assert mutexes.get_mutex(task.id) is None
        else:
            assert (
                await monitor._idle_check_handler._recovery.recover_completed_turn(
                    interactive,
                    tmux_name="seat",
                    session_id=run.child_session_id,
                    transcript_path="seat.jsonl",
                    snapshot=_snapshot(),
                    idle_timeout_seconds=10,
                )
                == 0
            )

    stored = agent_run_manager.get(run.id)
    assert stored is not None
    assert stored.status == "running"
    assert _runtime_of(monitor).write_log == []

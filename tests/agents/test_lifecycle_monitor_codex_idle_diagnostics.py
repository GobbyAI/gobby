from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import pytest

from gobby.agents.idle_check_handler import REASONING_WATCHDOG_CONTINUATION, IdleCheckHandler
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


@pytest.fixture
def agent_run_manager(temp_db: HubDatabase) -> LocalAgentRunManager:
    return LocalAgentRunManager(temp_db)


def _make_terminal_run(
    agent_run_manager: LocalAgentRunManager,
    parent_session: dict,
    *,
    child_session_id: str,
    run_id: str,
    tmux_session_name: str,
    task_id: str | None = None,
    agent_name: str | None = None,
) -> AgentRun:
    run = agent_run_manager.create(
        parent_session_id=parent_session["id"],
        provider="codex",
        prompt="test",
        run_id=run_id,
        child_session_id=child_session_id,
        task_id=task_id,
        agent_name=agent_name,
    )
    agent_run_manager.start(run.id)
    agent_run_manager.update_runtime(run.id, tmux_session_name=tmux_session_name)
    stored_run = agent_run_manager.get(run.id)
    assert stored_run is not None
    return stored_run


def _write_codex_transcript(path: Path) -> None:
    lines = [
        json.dumps(
            {
                "timestamp": "2026-04-25T03:48:20.004Z",
                "type": "response_item",
                "payload": {
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {
                        "type": "search",
                        "query": "pg_search docs",
                        "queries": ["pg_search docs"],
                    },
                },
            }
        ),
        json.dumps(
            {
                "timestamp": "2026-04-25T03:48:21.004Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call_1",
                    "name": "apply_patch",
                    "input": "*** Begin Patch\n*** End Patch\n",
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_codex_reasoning_transcript(path: Path) -> None:
    lines = [
        json.dumps(
            {
                "timestamp": "2026-04-25T03:48:20.004Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call_diff",
                    "output": '{"success": true}',
                },
            }
        ),
        json.dumps(
            {
                "timestamp": "2026-04-25T03:56:20.004Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [
                        {
                            "type": "summary_text",
                            "text": "Reviewing evidence before approving.",
                        }
                    ],
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_read_recent_codex_response_items_keeps_tail(tmp_path: Path) -> None:
    transcript_path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(transcript_path)

    items = await IdleCheckHandler._read_recent_codex_response_items(str(transcript_path), limit=1)

    assert len(items) == 1
    assert items[0]["payload_type"] == "custom_tool_call"


@pytest.mark.asyncio
async def test_idle_reprompt_logs_codex_response_items(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    config = TmuxConfig(idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2)
    monitor = AgentLifecycleMonitor(
        agent_run_manager=agent_run_manager,
        db=temp_db,
        session_manager=session_manager,
        check_interval_seconds=1.0,
        tmux_config=config,
    )

    parent = session_manager.register(
        external_id="parent-session",
        machine_id="machine-1",
        source="claude",
        project_id=sample_project["id"],
    )
    transcript_path = tmp_path / "codex-rollout.jsonl"
    _write_codex_transcript(transcript_path)
    child = session_manager.register(
        external_id="codex-child",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
        transcript_path=str(transcript_path),
    )
    stale_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    temp_db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (stale_time, child.id))

    run = _make_terminal_run(
        agent_run_manager,
        parent.to_dict(),
        child_session_id=child.id,
        run_id="run-codex-idle",
        tmux_session_name="gobby-codex-idle",
    )
    state = monitor._idle_detector.get_state(run.id)
    state.first_idle_at = time.monotonic() - 120

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True),
        patch("gobby.agents.idle_check_handler.logger.warning") as mock_warning,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    assert any(
        call.args
        and call.args[0] == "Codex idle diagnostic for run %s (%s) session %s: %s"
        and "web_search_call" in str(call.args[4])
        for call in mock_warning.call_args_list
    )


@pytest.mark.asyncio
async def test_idle_reasoning_watchdog_interrupts_codex_and_records_task_event(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Review watchdog target",
        task_type="bug",
    )
    task_manager.initialize_task_manifest(task.id)
    task_manager.stage_states.start_stage(task.id, "development", by_session_id="worker")
    task_manager.submit_for_review(task.id, "development", by_session_id="worker")

    config = TmuxConfig(idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2)
    monitor = AgentLifecycleMonitor(
        agent_run_manager=agent_run_manager,
        db=temp_db,
        session_manager=session_manager,
        task_manager=task_manager,
        check_interval_seconds=1.0,
        tmux_config=config,
    )

    parent = session_manager.register(
        external_id="parent-session-reasoning",
        machine_id="machine-1",
        source="claude",
        project_id=sample_project["id"],
    )
    transcript_path = tmp_path / "codex-reasoning.jsonl"
    _write_codex_reasoning_transcript(transcript_path)
    child = session_manager.register(
        external_id="codex-child-reasoning",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
        transcript_path=str(transcript_path),
    )
    stale_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    temp_db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (stale_time, child.id))

    run = _make_terminal_run(
        agent_run_manager,
        parent.to_dict(),
        child_session_id=child.id,
        run_id="run-codex-reasoning",
        tmux_session_name="gobby-codex-reasoning",
        task_id=task.id,
        agent_name="qa-reviewer",
    )
    state = monitor._idle_detector.get_state(run.id)
    state.first_idle_at = time.monotonic() - 120

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value=""),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch("gobby.agents.idle_check_handler.asyncio.sleep", new_callable=AsyncMock),
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    mock_send.assert_has_awaits(
        [
            call("gobby-codex-reasoning", "C-c", literal=False),
            call("gobby-codex-reasoning", REASONING_WATCHDOG_CONTINUATION + "\n"),
        ]
    )
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 1

    events = task_manager.lifecycle_events.list_lifecycle_events(task.id, newest_first=True)
    assert any(
        event.by_actor == "agent_idle_watchdog"
        and event.reason.startswith("agent_idle_watchdog:reasoning_interrupt")
        and "run_id=run-codex-reasoning" in event.reason
        for event in events
    )


@pytest.mark.asyncio
async def test_idle_failure_logs_codex_response_items(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    config = TmuxConfig(idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2)
    monitor = AgentLifecycleMonitor(
        agent_run_manager=agent_run_manager,
        db=temp_db,
        session_manager=session_manager,
        check_interval_seconds=1.0,
        tmux_config=config,
    )

    parent = session_manager.register(
        external_id="parent-session-fail",
        machine_id="machine-1",
        source="claude",
        project_id=sample_project["id"],
    )
    transcript_path = tmp_path / "codex-rollout-fail.jsonl"
    _write_codex_transcript(transcript_path)
    child = session_manager.register(
        external_id="codex-child-fail",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
        transcript_path=str(transcript_path),
    )
    stale_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    temp_db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (stale_time, child.id))

    run = _make_terminal_run(
        agent_run_manager,
        parent.to_dict(),
        child_session_id=child.id,
        run_id="run-codex-fail",
        tmux_session_name="gobby-codex-fail",
    )
    state = monitor._idle_detector.get_state(run.id)
    state.reprompt_count = 2

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(monitor._tmux, "kill_session", new_callable=AsyncMock, return_value=True),
        patch("gobby.agents.idle_check_handler.logger.warning") as mock_warning,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    assert any(
        call.args
        and call.args[0] == "Codex idle diagnostic for run %s (%s) session %s: %s"
        and "custom_tool_call" in str(call.args[4])
        for call in mock_warning.call_args_list
    )

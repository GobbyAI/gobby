from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.database import LocalDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


@pytest.fixture
def agent_run_manager(temp_db: LocalDatabase) -> LocalAgentRunManager:
    return LocalAgentRunManager(temp_db)


def _make_terminal_run(
    agent_run_manager: LocalAgentRunManager,
    parent_session: dict,
    *,
    child_session_id: str,
    run_id: str,
    tmux_session_name: str,
) -> AgentRun:
    run = agent_run_manager.create(
        parent_session_id=parent_session["id"],
        provider="codex",
        prompt="test",
        run_id=run_id,
        child_session_id=child_session_id,
    )
    agent_run_manager.start(run.id)
    agent_run_manager.update_runtime(run.id, tmux_session_name=tmux_session_name)
    return agent_run_manager.get(run.id)  # type: ignore[return-value]


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


def test_read_recent_codex_response_items_keeps_tail(tmp_path: Path) -> None:
    transcript_path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(transcript_path)

    items = AgentLifecycleMonitor._read_recent_codex_response_items(str(transcript_path), limit=1)

    assert len(items) == 1
    assert items[0]["payload_type"] == "custom_tool_call"


@pytest.mark.asyncio
async def test_idle_reprompt_logs_codex_response_items(
    temp_db: LocalDatabase,
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
        patch("gobby.agents.lifecycle_monitor.logger.warning") as mock_warning,
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
async def test_idle_failure_logs_codex_response_items(
    temp_db: LocalDatabase,
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
        patch("gobby.agents.lifecycle_monitor.logger.warning") as mock_warning,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    assert any(
        call.args
        and call.args[0] == "Codex idle diagnostic for run %s (%s) session %s: %s"
        and "custom_tool_call" in str(call.args[4])
        for call in mock_warning.call_args_list
    )

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.watchdog import WatchdogReaderRegistry, WatchdogTranscriptSnapshot
from gobby.agents.watchdog.recovery import REASONING_WATCHDOG_CONTINUATION
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

from .detection_test_support import BundledDetectionRegistry
from tests.agents.terminal_fixtures import make_live_terminal

DETECTION_REGISTRY = cast(DetectionManifestRegistry, BundledDetectionRegistry())
pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def agent_run_manager(temp_db: HubDatabase) -> LocalAgentRunManager:
    return LocalAgentRunManager(temp_db)


def _make_terminal_run(
    agent_run_manager: LocalAgentRunManager,
    parent_session: dict[str, Any],
    *,
    child_session_id: str,
    run_id: str,
    terminal_id: str,
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
    agent_run_manager.update_runtime(run.id)
    _live_run = agent_run_manager.get(run.id)
    assert _live_run is not None
    make_live_terminal(_live_run, db=agent_run_manager.db, session_name=terminal_id)


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
                    "encrypted_content": "encrypted-reasoning-secret",
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
async def test_idle_reprompt_logs_watchdog_snapshot(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    config = TmuxConfig(
        idle_check_enabled=True,
        idle_timeout_seconds=10,
        max_reprompt_attempts=2,
        reasoning_watchdog_settle_seconds=0,
    )
    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        session_manager=session_manager,
        check_interval_seconds=1.0,
        tmux_config=config,
    )

    parent = session_manager.register(
        external_id="parent-session",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=sample_project["id"],
    )
    transcript_path = tmp_path / "codex-rollout.jsonl"
    _write_codex_transcript(transcript_path)
    child = session_manager.register(
        external_id="codex-child",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
        transcript_path=str(transcript_path),
    )
    stale_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    temp_db.execute("UPDATE sessions SET updated_at = %s WHERE id = %s", (stale_time, child.id))

    run = _make_terminal_run(
        agent_run_manager,
        parent.to_dict(),
        child_session_id=child.id,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1002",
        terminal_id="gobby-codex-idle",
    )
    state = monitor._idle_detector.get_state(run.id)
    state.first_idle_at = time.monotonic() - 360

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True),
        patch("gobby.agents.watchdog.recovery.logger.warning") as mock_warning,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    diagnostics = [
        str(log_call.args[5])
        for log_call in mock_warning.call_args_list
        if log_call.args
        and log_call.args[0] == "Watchdog idle diagnostic for %s run %s (%s) session %s: %s"
    ]
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert "web_search_call" in diagnostic
    assert "custom_tool_call" in diagnostic
    assert "event_type" in diagnostic
    assert "timestamp" in diagnostic
    assert "pg_search docs" not in diagnostic
    assert "apply_patch" not in diagnostic
    assert "*** Begin Patch" not in diagnostic


@pytest.mark.asyncio
async def test_idle_reasoning_watchdog_interrupts_supported_reader_and_records_task_event(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Review watchdog target",
        task_type="bug",
        validation_criteria="Test task completion is observable.",
    )
    task_manager.initialize_task_manifest(task.id)
    task_manager.stage_states.start_stage(task.id, "development", by_session_id="worker")
    task_manager.submit_for_review(task.id, "development", by_session_id="worker")

    config = TmuxConfig(idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2)
    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        session_manager=session_manager,
        task_manager=task_manager,
        check_interval_seconds=1.0,
        tmux_config=config,
    )

    parent = session_manager.register(
        external_id="parent-session-reasoning",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=sample_project["id"],
    )
    transcript_path = tmp_path / "codex-reasoning.jsonl"
    _write_codex_reasoning_transcript(transcript_path)
    child = session_manager.register(
        external_id="codex-child-reasoning",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
        transcript_path=str(transcript_path),
    )
    stale_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    temp_db.execute("UPDATE sessions SET updated_at = %s WHERE id = %s", (stale_time, child.id))

    run = _make_terminal_run(
        agent_run_manager,
        parent.to_dict(),
        child_session_id=child.id,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1003",
        terminal_id="gobby-codex-reasoning",
        task_id=task.id,
        agent_name="qa-reviewer",
    )
    state = monitor._idle_detector.get_state(run.id)
    state.first_idle_at = time.monotonic() - 360

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value=""),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch("gobby.agents.watchdog.recovery.logger.warning") as mock_warning,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    mock_send.assert_has_awaits(
        [
            call("gobby-codex-reasoning", "C-c", literal=False),
            call("gobby-codex-reasoning", REASONING_WATCHDOG_CONTINUATION),
            call("gobby-codex-reasoning", "Enter", literal=False),
        ]
    )
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 1
    watchdog_diagnostics = [
        str(log_call.args[4])
        for log_call in mock_warning.call_args_list
        if log_call.args
        and log_call.args[0] == "Reasoning watchdog interrupting %s run %s session %s: %s"
    ]
    assert len(watchdog_diagnostics) == 1
    assert "reasoning" in watchdog_diagnostics[0]
    assert "event_type" in watchdog_diagnostics[0]
    assert "encrypted-reasoning-secret" not in watchdog_diagnostics[0]
    assert "Reviewing evidence before approving" not in watchdog_diagnostics[0]

    events = task_manager.lifecycle_events.list_lifecycle_events(task.id, newest_first=True)
    assert any(
        event.by_actor == "agent_idle_watchdog"
        and event.reason.startswith("agent_idle_watchdog:reasoning_interrupt")
        and "run_id=dddddddd-dddd-4ddd-8ddd-dddddddd1003" in event.reason
        for event in events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "droid", "grok", "qwen"])
async def test_reasoning_watchdog_does_not_interrupt_unsupported_readers(
    provider: str,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    agent_run_manager: LocalAgentRunManager,
) -> None:
    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        session_manager=session_manager,
        check_interval_seconds=1.0,
        tmux_config=TmuxConfig(
            reasoning_watchdog_interrupt_enabled=True,
            reasoning_watchdog_settle_seconds=0,
        ),
    )
    reader = WatchdogReaderRegistry().for_provider(provider)
    assert reader is not None
    assert reader.supports_reasoning_interrupt is False
    run = MagicMock(spec=AgentRun)
    run.id = f"{provider}-run"
    run.provider = provider
    snapshot = WatchdogTranscriptSnapshot(
        provider=provider,
        latest_activity_kind="reasoning",
    )

    with patch.object(monitor._tmux, "send_keys", new_callable=AsyncMock) as send_keys:
        recovered = await monitor._idle_check_handler._recovery._recover_reasoning_idle(
            run,
            tmux_name=f"gobby-{provider}",
            session=None,
            session_id=None,
            reader=reader,
            snapshot=snapshot,
        )

    assert recovered is False
    send_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_failure_logs_watchdog_snapshot(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    config = TmuxConfig(idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2)
    monitor = AgentLifecycleMonitor(
        detection_registry=DETECTION_REGISTRY,
        agent_run_manager=agent_run_manager,
        db=temp_db,
        session_manager=session_manager,
        check_interval_seconds=1.0,
        tmux_config=config,
    )

    parent = session_manager.register(
        external_id="parent-session-fail",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=sample_project["id"],
    )
    transcript_path = tmp_path / "codex-rollout-fail.jsonl"
    _write_codex_transcript(transcript_path)
    child = session_manager.register(
        external_id="codex-child-fail",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
        transcript_path=str(transcript_path),
    )
    stale_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    temp_db.execute("UPDATE sessions SET updated_at = %s WHERE id = %s", (stale_time, child.id))

    run = _make_terminal_run(
        agent_run_manager,
        parent.to_dict(),
        child_session_id=child.id,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1004",
        terminal_id="gobby-codex-fail",
    )
    state = monitor._idle_detector.get_state(run.id)
    state.reprompt_count = 2

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(monitor._tmux, "kill_session", new_callable=AsyncMock, return_value=True),
        patch("gobby.agents.watchdog.recovery.logger.warning") as mock_warning,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    assert any(
        log_call.args
        and log_call.args[0] == "Watchdog idle diagnostic for %s run %s (%s) session %s: %s"
        and "custom_tool_call" in str(log_call.args[5])
        for log_call in mock_warning.call_args_list
    )

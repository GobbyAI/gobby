from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import psycopg
import pytest

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.idle_detector import IdleDetector
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.watchdog.models import CapacityRecoveryState, CompletedTurnRecoveryState
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.step_context import IncompleteStepWorkflow, StepWorkflowContext

from .detection_test_support import BundledDetectionRegistry
from tests.agents.terminal_fixtures import make_live_terminal

DETECTION_REGISTRY = cast(DetectionManifestRegistry, BundledDetectionRegistry())
pytestmark = pytest.mark.unit
_CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."
_CAPACITY_PANE = "\x1b[31mSelected model is at\ncapacity. Please try a different model.\x1b[0m\n›\n"

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


def _write_codex_lifecycle_transcript(
    path: Path,
    *,
    lifecycle_events: tuple[str, ...] = ("task_started", "task_complete"),
    response_payload_type: str | None = None,
    age_seconds: int = 120,
    malformed_tail: bool = False,
) -> None:
    timestamp = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    lines: list[str] = []
    if response_payload_type is not None:
        lines.append(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": response_payload_type,
                        "encrypted_content": "encrypted-reasoning-secret",
                        "summary": [{"type": "summary_text", "text": "private reasoning"}],
                    },
                }
            )
        )
    lines.extend(
        json.dumps(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {
                    "type": lifecycle_event,
                    "last_agent_message": "prompt-and-tool-secret",
                },
            }
        )
        for lifecycle_event in lifecycle_events
    )
    if malformed_tail:
        lines.append('{"timestamp":"unterminated"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_claude_lifecycle_transcript(
    path: Path,
    *,
    obsolete_completion: bool = False,
    age_seconds: int = 120,
) -> None:
    timestamp = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
    records: list[dict[str, object]] = [
        {
            "type": "user",
            "timestamp": timestamp,
            "message": {"role": "user", "content": "continue"},
        },
        {
            "type": "assistant",
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "private reasoning"}],
            },
        },
        {
            "type": "system",
            "subtype": "turn_duration",
            "timestamp": timestamp,
            "durationMs": 1200,
            "messageCount": 2,
        },
    ]
    if obsolete_completion:
        records.extend(
            [
                {
                    "type": "user",
                    "timestamp": timestamp,
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_next",
                                "content": "private tool output",
                            }
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": timestamp,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "thinking", "thinking": "new turn"}],
                    },
                },
            ]
        )
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _write_grok_lifecycle_transcript(path: Path, *, age_seconds: int = 120) -> None:
    timestamp = time.time() - age_seconds
    records = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "grok-session",
                "update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"text": "continue"},
                },
                "_meta": {"agentTimestampMs": int(timestamp * 1000)},
            },
            "timestamp": timestamp,
        },
        {
            "jsonrpc": "2.0",
            "method": "_x.ai/session/update",
            "params": {
                "sessionId": "grok-session",
                "update": {
                    "sessionUpdate": "turn_completed",
                    "prompt_id": "prompt-id",
                    "stop_reason": "end_turn",
                },
                "_meta": {"agentTimestampMs": int(timestamp * 1000)},
            },
            "timestamp": timestamp,
        },
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _write_droid_lifecycle_transcript(path: Path, *, secret: str) -> None:
    timestamp = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    records = [
        {
            "type": "message",
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": secret}],
            },
        }
    ]
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _append_codex_capacity_turn(
    path: Path,
    *,
    message: str = _CAPACITY_MESSAGE,
    error_info: str = "server_overloaded",
    model_output_payload_type: str | None = None,
    lifecycle_suffix: tuple[str, ...] = (),
    malformed_tail: bool = False,
) -> None:
    timestamp = datetime.now(UTC).isoformat()
    lines = [
        json.dumps(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "task_started"},
            }
        ),
        json.dumps(
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": "continue"},
            }
        ),
    ]
    if model_output_payload_type is not None:
        lines.append(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {"type": model_output_payload_type},
                }
            )
        )
    lines.extend(
        [
            json.dumps(
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {
                        "type": "error",
                        "message": message,
                        "codex_error_info": error_info,
                    },
                }
            ),
            json.dumps(
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {"type": "task_complete"},
                }
            ),
        ]
    )
    lines.extend(
        json.dumps(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": lifecycle_event},
            }
        )
        for lifecycle_event in lifecycle_suffix
    )
    if malformed_tail:
        lines.append('{"timestamp":"unterminated"')
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _make_idle_monitor_run(
    *,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    run_id: str,
    transcript_path: Path | None,
    child_source: str = "codex",
    session_age_seconds: int = 120,
    task_manager: LocalTaskManager | None = None,
    max_reprompt_attempts: int = 2,
) -> tuple[AgentLifecycleMonitor, AgentRun]:
    config = TmuxConfig(
        idle_check_enabled=True,
        idle_timeout_seconds=10,
        idle_reprompt_delay_seconds=300,
        max_reprompt_attempts=max_reprompt_attempts,
        reasoning_watchdog_settle_seconds=0,
    )
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
        external_id=f"parent-{run_id}",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=sample_project["id"],
    )
    child = session_manager.register(
        external_id=f"child-{run_id}",
        machine_id="21000000-0000-4000-8000-000000000001",
        source=child_source,
        project_id=sample_project["id"],
        transcript_path=str(transcript_path) if transcript_path is not None else None,
    )
    updated_at = (datetime.now(UTC) - timedelta(seconds=session_age_seconds)).isoformat()
    temp_db.execute("UPDATE sessions SET updated_at = %s WHERE id = %s", (updated_at, child.id))
    run = _make_terminal_run(
        agent_run_manager,
        parent.to_dict(),
        child_session_id=child.id,
        run_id=run_id,
        terminal_id=f"gobby-{run_id[-4:]}",
    )
    return monitor, run


@pytest.mark.asyncio
async def test_completed_turn_reprompts_after_base_timeout_before_semantic_delay(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-completed-turn.jsonl"
    _write_codex_lifecycle_transcript(
        transcript_path,
        response_payload_type="reasoning",
    )
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1010",
        transcript_path=transcript_path,
    )
    assert monitor._idle_detector.get_state(run.id).first_idle_at is None

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch.object(
            monitor._idle_check_handler._recovery,
            "_idle_reprompt_message",
            new_callable=AsyncMock,
            return_value="workflow-aware continuation",
        ) as mock_message,
        patch.object(
            monitor._idle_check_handler._recovery,
            "_record_watchdog_task_event",
            new_callable=AsyncMock,
        ) as mock_audit,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    mock_message.assert_awaited_once()
    mock_send.assert_has_awaits(
        [
            call(run.terminal_id, "Escape", literal=False),
            call(run.terminal_id, "workflow-aware continuation"),
            call(run.terminal_id, "Enter", literal=False),
        ]
    )
    assert all(awaited.args[1] != "C-c" for awaited in mock_send.await_args_list)
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 1
    mock_audit.assert_awaited_once_with(
        run,
        action="completed_turn_reprompt",
        session_id=run.child_session_id,
        detail="latest_turn_kind=completed",
    )


@pytest.mark.asyncio
async def test_claude_turn_duration_reprompts_after_base_timeout(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "claude-completed-turn.jsonl"
    _write_claude_lifecycle_transcript(transcript_path)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1014",
        transcript_path=transcript_path,
        child_source="claude",
    )

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch.object(
            monitor._idle_check_handler._recovery,
            "_idle_reprompt_message",
            new_callable=AsyncMock,
            return_value="workflow-aware continuation",
        ),
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    mock_send.assert_has_awaits(
        [
            call(run.terminal_id, "Escape", literal=False),
            call(run.terminal_id, "workflow-aware continuation"),
            call(run.terminal_id, "Enter", literal=False),
        ]
    )


@pytest.mark.asyncio
async def test_grok_turn_completed_reprompts_and_records_watchdog_event(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "grok-completed-turn.jsonl"
    _write_grok_lifecycle_transcript(transcript_path)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1016",
        transcript_path=transcript_path,
        child_source="grok",
    )

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch.object(
            monitor._idle_check_handler._recovery,
            "_idle_reprompt_message",
            new_callable=AsyncMock,
            return_value="workflow-aware continuation",
        ) as mock_message,
        patch.object(
            monitor._idle_check_handler._recovery,
            "_record_watchdog_task_event",
            new_callable=AsyncMock,
        ) as mock_audit,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    mock_message.assert_awaited_once()
    mock_send.assert_has_awaits(
        [
            call(run.terminal_id, "Escape", literal=False),
            call(run.terminal_id, "workflow-aware continuation"),
            call(run.terminal_id, "Enter", literal=False),
        ]
    )
    assert all(awaited.args[1] != "C-c" for awaited in mock_send.await_args_list)
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 1
    mock_audit.assert_awaited_once_with(
        run,
        action="completed_turn_reprompt",
        session_id=run.child_session_id,
        detail="latest_turn_kind=completed",
    )


@pytest.mark.asyncio
async def test_claude_new_user_record_suppresses_obsolete_completion_recovery(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "claude-active-next-turn.jsonl"
    _write_claude_lifecycle_transcript(transcript_path, obsolete_completion=True)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1015",
        transcript_path=transcript_path,
        child_source="claude",
    )

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 0
    mock_send.assert_not_awaited()
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 0


@pytest.mark.asyncio
async def test_fresh_task_complete_waits_for_base_timeout_before_any_recovery(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-fresh-completed-turn.jsonl"
    _write_codex_lifecycle_transcript(
        transcript_path,
        response_payload_type="reasoning",
        age_seconds=1,
    )
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1016",
        transcript_path=transcript_path,
    )
    monitor._idle_detector.get_state(run.id).first_idle_at = time.monotonic() - 360

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 0
    mock_send.assert_not_awaited()
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 0


@pytest.mark.asyncio
async def test_fresh_capacity_error_immediately_sends_workflow_aware_reprompt(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-fresh-capacity.jsonl"
    _append_codex_capacity_turn(transcript_path)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1020",
        transcript_path=transcript_path,
        session_age_seconds=1,
    )

    with (
        patch.object(
            monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value=_CAPACITY_PANE
        ),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch.object(
            monitor._idle_check_handler._recovery,
            "_idle_reprompt_message",
            new_callable=AsyncMock,
            return_value="workflow-aware continuation",
        ),
        patch.object(
            monitor._idle_check_handler._recovery,
            "_record_watchdog_task_event",
            new_callable=AsyncMock,
        ) as mock_audit,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 1
    state = monitor._idle_check_handler._recovery._capacity_recovery[run.id]
    assert state.successful_reprompts == 1
    mock_send.assert_has_awaits(
        [
            call(run.terminal_id, "Escape", literal=False),
            call(run.terminal_id, "workflow-aware continuation"),
            call(run.terminal_id, "Enter", literal=False),
        ]
    )
    mock_audit.assert_awaited_once_with(
        run,
        action="capacity_reprompt",
        session_id=run.child_session_id,
        detail="capacity_error=server_overloaded;attempt=1/2",
    )


@pytest.mark.asyncio
async def test_stale_session_discovers_transcript_without_updating_session_row(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-discovered-capacity.jsonl"
    _append_codex_capacity_turn(transcript_path)
    run_id = "dddddddd-dddd-4ddd-8ddd-dddddddd1021"
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id=run_id,
        transcript_path=None,
    )
    assert run.child_session_id is not None
    session_before = session_manager.get(run.child_session_id)
    assert session_before is not None

    with (
        patch(
            "gobby.agents.watchdog.transcript_resolver.find_transcript_on_disk",
            return_value=str(transcript_path),
        ) as mock_discover,
        patch.object(
            monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value=_CAPACITY_PANE
        ),
        patch.object(monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True),
        patch.object(
            monitor._idle_check_handler._recovery,
            "_idle_reprompt_message",
            new_callable=AsyncMock,
            return_value="workflow-aware continuation",
        ),
        patch.object(
            monitor._idle_check_handler._recovery,
            "_record_watchdog_task_event",
            new_callable=AsyncMock,
        ),
    ):
        handled = await monitor.check_idle_agents()

    session_after = session_manager.get(run.child_session_id)
    assert session_after is not None
    assert handled == 1
    assert session_after.transcript_path is None
    assert session_after.updated_at == session_before.updated_at
    mock_discover.assert_called_once_with(
        "codex",
        f"child-{run_id}",
        owner_machine_id=LOCAL_MACHINE_ID,
        local_machine_id=LOCAL_MACHINE_ID,
    )


@pytest.mark.asyncio
async def test_fresh_non_capacity_session_skips_transcript_resolution(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
) -> None:
    monitor, _run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1022",
        transcript_path=None,
        session_age_seconds=1,
    )

    with (
        patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            return_value="active output\n",
        ),
        patch.object(
            monitor._idle_check_handler._transcript_resolver,
            "resolve",
            new_callable=AsyncMock,
        ) as mock_resolve,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 0
    mock_resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_capacity_pane_text_requires_structured_transcript_confirmation(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-capacity-pane-only.jsonl"
    _write_codex_lifecycle_transcript(transcript_path, age_seconds=1)
    monitor, _run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1021",
        transcript_path=transcript_path,
        session_age_seconds=1,
    )

    with (
        patch.object(
            monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value=_CAPACITY_PANE
        ),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 0
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_capacity_reprompt_retries_failed_send_and_deduplicates_success(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-capacity-dedupe.jsonl"
    _append_codex_capacity_turn(transcript_path)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1022",
        transcript_path=transcript_path,
        session_age_seconds=1,
    )

    with (
        patch.object(
            monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value=_CAPACITY_PANE
        ),
        patch.object(
            monitor._tmux,
            "send_keys",
            new_callable=AsyncMock,
            side_effect=[True, False, True, True, True],
        ) as mock_send,
        patch.object(
            monitor._idle_check_handler._recovery,
            "_idle_reprompt_message",
            new_callable=AsyncMock,
            return_value="continue",
        ),
        patch.object(
            monitor._idle_check_handler._recovery,
            "_record_watchdog_task_event",
            new_callable=AsyncMock,
        ) as mock_audit,
    ):
        first = await monitor.check_idle_agents()
        second = await monitor.check_idle_agents()
        duplicate = await monitor.check_idle_agents()

    assert (first, second, duplicate) == (0, 1, 0)
    assert mock_send.await_count == 5
    mock_audit.assert_awaited_once()
    state = monitor._idle_check_handler._recovery._capacity_recovery[run.id]
    assert state.successful_reprompts == 1


@pytest.mark.asyncio
async def test_capacity_reprompts_are_bounded_across_user_only_retry_turns(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-capacity-bounded.jsonl"
    _append_codex_capacity_turn(transcript_path)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1023",
        transcript_path=transcript_path,
        session_age_seconds=1,
    )

    with (
        patch.object(
            monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value=_CAPACITY_PANE
        ),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch.object(monitor._tmux, "kill_session", new_callable=AsyncMock, return_value=True),
        patch.object(
            monitor._idle_check_handler._recovery,
            "_record_watchdog_task_event",
            new_callable=AsyncMock,
        ) as mock_audit,
    ):
        first = await monitor.check_idle_agents()
        _append_codex_capacity_turn(transcript_path)
        second = await monitor.check_idle_agents()
        _append_codex_capacity_turn(transcript_path)
        exhausted = await monitor.check_idle_agents()

    assert (first, second, exhausted) == (1, 1, 1)
    assert mock_send.await_count == 6
    assert mock_audit.await_count == 2
    updated_run = agent_run_manager.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "error"


@pytest.mark.asyncio
async def test_capacity_retry_budget_resets_after_model_output(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-capacity-progress.jsonl"
    _append_codex_capacity_turn(transcript_path)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1024",
        transcript_path=transcript_path,
        session_age_seconds=1,
    )

    with (
        patch.object(
            monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value=_CAPACITY_PANE
        ),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch.object(
            monitor._tmux, "kill_session", new_callable=AsyncMock, return_value=True
        ) as mock_kill,
        patch.object(
            monitor._idle_check_handler._recovery,
            "_record_watchdog_task_event",
            new_callable=AsyncMock,
        ),
    ):
        await monitor.check_idle_agents()
        _append_codex_capacity_turn(transcript_path)
        await monitor.check_idle_agents()
        _append_codex_capacity_turn(transcript_path, model_output_payload_type="reasoning")
        recovered = await monitor.check_idle_agents()

    assert recovered == 1
    assert mock_send.await_count == 9
    mock_kill.assert_not_awaited()
    state = monitor._idle_check_handler._recovery._capacity_recovery[run.id]
    assert state.successful_reprompts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "child_source", "lifecycle_events", "malformed_tail"),
    [
        ("later-start", "codex", ("task_complete", "task_started"), False),
        ("provider-mismatch", "claude", ("task_complete",), False),
        ("malformed", "codex", ("task_complete",), True),
    ],
)
async def test_completed_turn_expedited_recovery_requires_conclusive_provider_marker(
    case: str,
    child_source: str,
    lifecycle_events: tuple[str, ...],
    malformed_tail: bool,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / f"codex-{case}.jsonl"
    _write_codex_lifecycle_transcript(
        transcript_path,
        lifecycle_events=lifecycle_events,
        malformed_tail=malformed_tail,
    )
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id={
            "later-start": "dddddddd-dddd-4ddd-8ddd-dddddddd1011",
            "provider-mismatch": "dddddddd-dddd-4ddd-8ddd-dddddddd1012",
            "malformed": "dddddddd-dddd-4ddd-8ddd-dddddddd1013",
        }[case],
        transcript_path=transcript_path,
        child_source=child_source,
    )

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 0
    mock_send.assert_not_awaited()
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 0


@pytest.mark.asyncio
async def test_unreadable_transcript_uses_existing_delayed_idle_reprompt(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1014",
        transcript_path=tmp_path / "missing-rollout.jsonl",
    )
    monitor._idle_detector.get_state(run.id).first_idle_at = time.monotonic() - 360

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch.object(
            monitor._idle_check_handler._recovery,
            "_idle_reprompt_message",
            new_callable=AsyncMock,
            return_value="delayed continuation",
        ),
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    mock_send.assert_has_awaits(
        [
            call(run.terminal_id, "Escape", literal=False),
            call(run.terminal_id, "delayed continuation"),
            call(run.terminal_id, "Enter", literal=False),
        ]
    )
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 1


@pytest.mark.asyncio
async def test_completed_turn_recovery_proceeds_despite_unsubmitted_input(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-unsubmitted.jsonl"
    _write_codex_lifecycle_transcript(transcript_path)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1015",
        transcript_path=transcript_path,
    )

    with (
        patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            return_value="❯ uv run pytest tests/foo.py\n",
        ),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch.object(
            monitor._idle_check_handler._recovery,
            "_idle_reprompt_message",
            new_callable=AsyncMock,
            return_value="completed continuation",
        ),
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    mock_send.assert_has_awaits(
        [
            call(run.terminal_id, "Escape", literal=False),
            call(run.terminal_id, "completed continuation"),
            call(run.terminal_id, "Enter", literal=False),
        ]
    )
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 1


@pytest.mark.asyncio
async def test_unsubmitted_input_still_suppresses_reprompt_without_completed_turn(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-unsubmitted-incomplete.jsonl"
    _write_codex_lifecycle_transcript(transcript_path, lifecycle_events=("task_started",))
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1019",
        transcript_path=transcript_path,
    )

    with (
        patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            return_value="❯ uv run pytest tests/foo.py\n",
        ),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 0
    mock_send.assert_not_awaited()
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 0


@pytest.mark.asyncio
async def test_recent_session_activity_still_checks_supported_capacity_pane(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-recent-session.jsonl"
    _write_codex_lifecycle_transcript(transcript_path)
    monitor, _run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1018",
        transcript_path=transcript_path,
        session_age_seconds=1,
    )

    with (
        patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            return_value="active output\n",
        ) as mock_capture,
        patch.object(monitor._tmux, "send_keys", new_callable=AsyncMock) as mock_send,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 0
    mock_capture.assert_awaited_once_with(_run.terminal_id, lines=15)
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_turn_recovery_retains_max_attempt_failure(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-max-attempts.jsonl"
    _write_codex_lifecycle_transcript(transcript_path)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1017",
        transcript_path=transcript_path,
    )
    monitor._idle_detector.get_state(run.id).reprompt_count = 2

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch.object(monitor._tmux, "kill_session", new_callable=AsyncMock, return_value=True),
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    mock_send.assert_not_awaited()
    updated_run = agent_run_manager.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "error"


@pytest.mark.asyncio
async def test_exhausted_recovery_completes_agent_parked_on_satisfied_exit_condition(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    """A terminal step the agent cannot act on is a completion, not a failure.

    Every bundled agent's terminate step allows only the gobby MCP proxy
    tools, so an agent whose proxy never started has no permitted action left
    once its exit condition is satisfied (#19097).
    """
    transcript_path = tmp_path / "codex-terminal-step.jsonl"
    _write_codex_lifecycle_transcript(transcript_path)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1030",
        transcript_path=transcript_path,
    )
    monitor._idle_detector.get_state(run.id).reprompt_count = 2
    terminate_context = StepWorkflowContext(
        workflow_name="expansion-qa-steps",
        current_step="terminate",
        description=None,
        status_message=None,
        exit_condition="current_step == 'terminate'",
    )

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch.object(monitor._tmux, "kill_session", new_callable=AsyncMock, return_value=True),
        patch(
            "gobby.agents.watchdog.recovery.get_active_step_workflow_context",
            return_value=terminate_context,
        ),
        patch(
            "gobby.agents.watchdog.recovery.first_incomplete_step_workflow",
            return_value=None,
        ) as mock_incomplete,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    mock_send.assert_not_awaited()
    mock_incomplete.assert_called_once()
    updated_run = agent_run_manager.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "success"
    assert updated_run.error is None
    assert "reached its exit condition" in (updated_run.result or "")


@pytest.mark.asyncio
async def test_exhausted_recovery_still_fails_when_exit_condition_is_unmet(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    """An unfinished workflow keeps the existing failure outcome."""
    transcript_path = tmp_path / "codex-midway-step.jsonl"
    _write_codex_lifecycle_transcript(transcript_path)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1031",
        transcript_path=transcript_path,
    )
    monitor._idle_detector.get_state(run.id).reprompt_count = 2
    qa_context = StepWorkflowContext(
        workflow_name="expansion-qa-steps",
        current_step="qa_check",
        description=None,
        status_message=None,
        exit_condition="current_step == 'terminate'",
    )
    incomplete = IncompleteStepWorkflow(
        workflow_name="expansion-qa-steps",
        current_step="qa_check",
        exit_condition="current_step == 'terminate'",
    )

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True),
        patch.object(monitor._tmux, "kill_session", new_callable=AsyncMock, return_value=True),
        patch(
            "gobby.agents.watchdog.recovery.get_active_step_workflow_context",
            return_value=qa_context,
        ),
        patch(
            "gobby.agents.watchdog.recovery.first_incomplete_step_workflow",
            return_value=incomplete,
        ),
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    updated_run = agent_run_manager.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "error"


@pytest.mark.asyncio
async def test_idle_reprompt_falls_back_when_step_context_lookup_fails(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
) -> None:
    """Unexpected step-context lookup errors fall back and log exception context."""
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
        external_id="parent-session-fallback",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=sample_project["id"],
    )
    child = session_manager.register(
        external_id="codex-child-fallback",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    run = _make_terminal_run(
        agent_run_manager,
        parent.to_dict(),
        child_session_id=child.id,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1001",
        terminal_id="gobby-codex-fallback",
    )

    with (
        patch(
            "gobby.agents.watchdog.recovery.get_active_step_workflow_context",
            side_effect=RuntimeError("context lookup failed"),
        ),
        patch("gobby.agents.watchdog.recovery.logger.exception") as mock_exception,
    ):
        message = await monitor._idle_check_handler._recovery._idle_reprompt_message(run)

    assert message == IdleDetector.REPROMPT_MESSAGE
    mock_exception.assert_called_once()
    assert (
        "Unexpected error loading active step workflow context" in mock_exception.call_args.args[0]
    )


@pytest.mark.asyncio
async def test_no_reader_provider_uses_shared_idle_path_without_transcript_read(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "agy-unread.jsonl"
    _write_codex_transcript(transcript_path)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1005",
        transcript_path=transcript_path,
        child_source="agy",
    )
    state = monitor._idle_detector.get_state(run.id)
    state.first_idle_at = time.monotonic() - 360

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch.object(
            monitor._idle_check_handler._recovery,
            "_idle_reprompt_message",
            new_callable=AsyncMock,
            return_value="shared continuation",
        ),
        patch(
            "gobby.agents.watchdog.codex.CodexTranscriptWatchdogReader.read",
            new_callable=AsyncMock,
        ) as mock_read,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    mock_read.assert_not_awaited()
    mock_send.assert_has_awaits(
        [
            call(run.terminal_id, "Escape", literal=False),
            call(run.terminal_id, "shared continuation"),
            call(run.terminal_id, "Enter", literal=False),
        ]
    )
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 1


@pytest.mark.asyncio
async def test_droid_diagnostics_only_reader_uses_shared_reprompt_and_redacted_log(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "droid-lifecycle-secret"
    transcript_path = tmp_path / "droid-diagnostic.jsonl"
    _write_droid_lifecycle_transcript(transcript_path, secret=secret)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1017",
        transcript_path=transcript_path,
        child_source="droid",
    )
    monitor._idle_detector.get_state(run.id).first_idle_at = time.monotonic() - 360

    with (
        caplog.at_level(logging.WARNING),
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux,
            "send_keys",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_send,
        patch.object(
            monitor._idle_check_handler._recovery,
            "_idle_reprompt_message",
            new_callable=AsyncMock,
            return_value="shared continuation",
        ),
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    mock_send.assert_has_awaits(
        [
            call(run.terminal_id, "Escape", literal=False),
            call(run.terminal_id, "shared continuation"),
            call(run.terminal_id, "Enter", literal=False),
        ]
    )
    diagnostic = "\n".join(caplog.messages)
    assert "Watchdog idle diagnostic for droid" in diagnostic
    assert '"latest_activity_kind": "reasoning"' in diagnostic
    assert secret not in diagnostic


@pytest.mark.asyncio
async def test_completed_turn_recovery_survives_activity_and_deduplicates_snapshot(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-repeated-completions.jsonl"
    _write_codex_lifecycle_transcript(transcript_path, age_seconds=120)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1020",
        transcript_path=transcript_path,
        max_reprompt_attempts=3,
    )
    handler = monitor._idle_check_handler

    with (
        patch.object(
            monitor._tmux,
            "capture_pane",
            new_callable=AsyncMock,
            side_effect=[
                "❯\n",
                "Processing files...\n",
                "Processing hook activity...\n",
                "❯\n",
            ],
        ),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
    ):
        assert await monitor.check_idle_agents() == 1
        state = handler._recovery._completed_turn_recovery[run.id]
        assert state.successful_reprompts == 1

        monitor._idle_detector.reset_idle(run.id)
        assert await monitor.check_idle_agents() == 0
        assert state.successful_reprompts == 1
        assert mock_send.await_count == 3

        temp_db.execute(
            "UPDATE sessions SET updated_at = %s WHERE id = %s",
            (datetime.now(UTC).isoformat(), run.child_session_id),
        )
        assert await monitor.check_idle_agents() == 0
        assert state.successful_reprompts == 1

        stale_time = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        temp_db.execute(
            "UPDATE sessions SET updated_at = %s WHERE id = %s",
            (stale_time, run.child_session_id),
        )
        _write_codex_lifecycle_transcript(transcript_path, age_seconds=121)
        monitor._idle_detector.reset_idle(run.id)
        assert await monitor.check_idle_agents() == 1

    assert state.successful_reprompts == 2
    assert mock_send.await_count == 6


@pytest.mark.asyncio
async def test_completed_turn_recovery_allows_budget_then_fails_without_step_workflow(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcript_path = tmp_path / "codex-bounded-completions.jsonl"
    _write_codex_lifecycle_transcript(transcript_path, age_seconds=120)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1021",
        transcript_path=transcript_path,
        max_reprompt_attempts=3,
    )
    handler = monitor._idle_check_handler
    caplog.set_level(logging.INFO)

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch.object(monitor._tmux, "kill_session", new_callable=AsyncMock, return_value=True),
    ):
        for attempt in range(3):
            _write_codex_lifecycle_transcript(
                transcript_path,
                age_seconds=120 + attempt,
            )
            monitor._idle_detector.reset_idle(run.id)
            assert await monitor.check_idle_agents() == 1

        state = handler._recovery._completed_turn_recovery[run.id]
        assert state.workflow_fingerprint == run.id
        assert state.successful_reprompts == 3

        _write_codex_lifecycle_transcript(transcript_path, age_seconds=123)
        monitor._idle_detector.reset_idle(run.id)
        assert await monitor.check_idle_agents() == 1

    assert mock_send.await_count == 9
    updated_run = agent_run_manager.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "error"
    assert run.id not in handler._recovery._completed_turn_recovery
    assert any(
        record.levelno == logging.INFO
        and "Watchdog idle diagnostic" in record.getMessage()
        and "recovering completed turn" in record.getMessage()
        for record in caplog.records
    )
    assert any(
        record.levelno == logging.ERROR
        and "completed another turn without workflow progress" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_completed_turn_recovery_completes_run_parked_on_satisfied_exit_condition(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exhausting completed-turn recovery on a finished workflow completes the run.

    This is the incident path: the agent kept completing turns at the
    terminate step but could not call end_agent_run because its MCP proxy
    never started (#19097).
    """
    transcript_path = tmp_path / "codex-terminal-budget.jsonl"
    _write_codex_lifecycle_transcript(transcript_path, age_seconds=120)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1032",
        transcript_path=transcript_path,
        max_reprompt_attempts=3,
    )
    caplog.set_level(logging.INFO)
    terminate_context = StepWorkflowContext(
        workflow_name="expansion-qa-steps",
        current_step="terminate",
        description=None,
        status_message=None,
        exit_condition="current_step == 'terminate'",
    )

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True),
        patch.object(monitor._tmux, "kill_session", new_callable=AsyncMock, return_value=True),
        patch(
            "gobby.agents.watchdog.recovery.get_active_step_workflow_context",
            return_value=terminate_context,
        ),
        patch(
            "gobby.agents.watchdog.recovery.first_incomplete_step_workflow",
            return_value=None,
        ),
    ):
        for attempt in range(3):
            _write_codex_lifecycle_transcript(transcript_path, age_seconds=120 + attempt)
            monitor._idle_detector.reset_idle(run.id)
            assert await monitor.check_idle_agents() == 1

        _write_codex_lifecycle_transcript(transcript_path, age_seconds=123)
        monitor._idle_detector.reset_idle(run.id)
        assert await monitor.check_idle_agents() == 1

    updated_run = agent_run_manager.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "success"
    assert not any(
        "completed another turn without workflow progress" in record.getMessage()
        for record in caplog.records
    )
    assert any(
        "exit condition already satisfied" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_completed_turn_recovery_budget_resets_when_workflow_step_advances(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-step-advance.jsonl"
    _write_codex_lifecycle_transcript(transcript_path, age_seconds=120)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1022",
        transcript_path=transcript_path,
        max_reprompt_attempts=2,
    )
    plan_context = StepWorkflowContext(
        workflow_name="developer-steps",
        current_step="plan",
        description=None,
        status_message=None,
        exit_condition=None,
    )
    build_context = StepWorkflowContext(
        workflow_name="developer-steps",
        current_step="build",
        description=None,
        status_message=None,
        exit_condition=None,
    )

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch(
            "gobby.agents.watchdog.recovery.get_active_step_workflow_context",
            return_value=plan_context,
        ) as mock_context,
    ):
        for attempt in range(2):
            _write_codex_lifecycle_transcript(
                transcript_path,
                age_seconds=120 + attempt,
            )
            monitor._idle_detector.reset_idle(run.id)
            assert await monitor.check_idle_agents() == 1

        state = monitor._idle_check_handler._recovery._completed_turn_recovery[run.id]
        assert state.successful_reprompts == 2
        mock_context.return_value = build_context
        _write_codex_lifecycle_transcript(transcript_path, age_seconds=122)
        monitor._idle_detector.reset_idle(run.id)
        assert await monitor.check_idle_agents() == 1

    assert mock_send.await_count == 9
    assert state.workflow_fingerprint == "developer-steps:build"
    assert state.successful_reprompts == 1
    updated_run = agent_run_manager.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "running"


@pytest.mark.asyncio
async def test_failed_completed_turn_prompt_submission_does_not_consume_attempt(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-failed-completion-prompt.jsonl"
    _write_codex_lifecycle_transcript(transcript_path)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1023",
        transcript_path=transcript_path,
    )
    handler = monitor._idle_check_handler

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux,
            "send_keys",
            new_callable=AsyncMock,
            side_effect=[True, True, False, True, True, True],
        ),
    ):
        assert await monitor.check_idle_agents() == 0
        state = handler._recovery._completed_turn_recovery[run.id]
        assert state.successful_reprompts == 0
        assert state.last_completion_identity is None

        monitor._idle_detector.reset_idle(run.id)
        assert await monitor.check_idle_agents() == 1

    assert state.successful_reprompts == 1
    assert state.last_completion_identity is not None


@pytest.mark.asyncio
async def test_completed_turn_lookup_failure_preserves_existing_recovery_budget(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-completion-context-failure.jsonl"
    _write_codex_lifecycle_transcript(transcript_path, age_seconds=120)
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1024",
        transcript_path=transcript_path,
        max_reprompt_attempts=1,
    )
    plan_context = StepWorkflowContext(
        workflow_name="developer-steps",
        current_step="plan",
        description=None,
        status_message=None,
        exit_condition=None,
    )

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(
            monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True
        ) as mock_send,
        patch.object(
            monitor._idle_check_handler._recovery,
            "_fail_idle_agent",
            new_callable=AsyncMock,
        ) as mock_fail,
        patch(
            "gobby.agents.watchdog.recovery.get_active_step_workflow_context",
            side_effect=[plan_context, psycopg.DatabaseError("lookup failed")],
        ),
    ):
        assert await monitor.check_idle_agents() == 1
        _write_codex_lifecycle_transcript(transcript_path, age_seconds=121)
        monitor._idle_detector.reset_idle(run.id)
        assert await monitor.check_idle_agents() == 1

    state = monitor._idle_check_handler._recovery._completed_turn_recovery[run.id]
    assert state.workflow_fingerprint == "developer-steps:plan"
    assert state.successful_reprompts == 1
    assert mock_send.await_count == 3
    mock_fail.assert_awaited_once()


@pytest.mark.asyncio
async def test_watchdog_task_audit_programming_error_propagates(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
) -> None:
    task_manager = MagicMock()
    task_manager.get_task.return_value = object()
    task_manager.lifecycle_events.record_lifecycle_event.side_effect = RuntimeError(
        "audit unavailable"
    )
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1027",
        transcript_path=None,
        task_manager=cast(LocalTaskManager, task_manager),
    )
    run.task_id = "task-1"

    with (
        patch("gobby.agents.watchdog.recovery.logger.warning") as mock_warning,
        pytest.raises(RuntimeError, match="audit unavailable"),
    ):
        await monitor._idle_check_handler._recovery._record_watchdog_task_event(
            run,
            action="completed_turn_reprompt",
            session_id=run.child_session_id,
            detail="latest_turn_kind=completed",
        )

    mock_warning.assert_not_called()


@pytest.mark.asyncio
async def test_tmux_cleanup_failure_preserves_watchdog_recovery_state(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
) -> None:
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1028",
        transcript_path=None,
    )
    handler = monitor._idle_check_handler
    handler._recovery._capacity_recovery[run.id] = CapacityRecoveryState(
        transcript_path="/failed-cleanup.jsonl"
    )
    handler._recovery._completed_turn_recovery[run.id] = CompletedTurnRecoveryState()
    handler._transcript_resolver._path_cache[(run.id, "codex", "failed-cleanup")] = (
        "/failed-cleanup.jsonl"
    )

    with (
        patch.object(monitor._tmux, "has_session", new_callable=AsyncMock, return_value=True),
        patch.object(
            monitor._tmux,
            "capture_full_pane",
            new_callable=AsyncMock,
            return_value="stalled",
        ),
        patch.object(
            monitor._tmux,
            "kill_session",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        await handler._recovery._fail_idle_agent(run, reason="test cleanup failure")

    updated_run = agent_run_manager.get(run.id)
    assert updated_run is not None
    assert updated_run.status == "running"
    assert run.id in handler._recovery._capacity_recovery
    assert run.id in handler._recovery._completed_turn_recovery
    assert any(key[0] == run.id for key in handler._transcript_resolver._path_cache)


@pytest.mark.asyncio
async def test_terminal_and_unmonitored_runs_clear_completed_turn_recovery_state(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
) -> None:
    monitor, inactive_run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1025",
        transcript_path=None,
    )
    handler = monitor._idle_check_handler
    handler._recovery._capacity_recovery[inactive_run.id] = CapacityRecoveryState(
        transcript_path="/inactive.jsonl"
    )
    handler._recovery._completed_turn_recovery[inactive_run.id] = CompletedTurnRecoveryState()
    handler._transcript_resolver._path_cache[(inactive_run.id, "codex", "inactive")] = (
        "/inactive.jsonl"
    )
    assert inactive_run.terminal_id is not None
    assert agent_run_manager.clear_terminal_id(
        inactive_run.id,
        inactive_run.terminal_id,
    )
    assert await monitor.check_idle_agents() == 0
    assert inactive_run.id not in handler._recovery._capacity_recovery
    assert inactive_run.id not in handler._recovery._completed_turn_recovery
    assert not any(key[0] == inactive_run.id for key in handler._transcript_resolver._path_cache)

    _, terminal_run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1026",
        transcript_path=None,
    )
    handler._recovery._capacity_recovery[terminal_run.id] = CapacityRecoveryState(
        transcript_path="/terminal.jsonl"
    )
    handler._recovery._completed_turn_recovery[terminal_run.id] = CompletedTurnRecoveryState()
    handler._transcript_resolver._path_cache[(terminal_run.id, "codex", "terminal")] = (
        "/terminal.jsonl"
    )
    assert agent_run_manager.complete(terminal_run.id) is not None
    assert await handler._handle_idle_check(terminal_run) == 0
    assert terminal_run.id not in handler._recovery._capacity_recovery
    assert terminal_run.id not in handler._recovery._completed_turn_recovery
    assert not any(key[0] == terminal_run.id for key in handler._transcript_resolver._path_cache)

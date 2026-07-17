from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import pytest

from gobby.agents.idle_check_handler import REASONING_WATCHDOG_CONTINUATION, IdleCheckHandler
from gobby.agents.idle_detector import IdleDetector
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit
_CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."
_CAPACITY_PANE = "\x1b[31mSelected model is at\ncapacity. Please try a different model.\x1b[0m\n›\n"


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
    sample_project: dict,
    agent_run_manager: LocalAgentRunManager,
    run_id: str,
    transcript_path: Path | None,
    child_source: str = "codex",
    session_age_seconds: int = 120,
    task_manager: LocalTaskManager | None = None,
) -> tuple[AgentLifecycleMonitor, AgentRun]:
    config = TmuxConfig(
        idle_check_enabled=True,
        idle_timeout_seconds=10,
        idle_reprompt_delay_seconds=300,
        max_reprompt_attempts=2,
        reasoning_watchdog_settle_seconds=0,
    )
    monitor = AgentLifecycleMonitor(
        agent_run_manager=agent_run_manager,
        db=temp_db,
        session_manager=session_manager,
        task_manager=task_manager,
        check_interval_seconds=1.0,
        tmux_config=config,
    )
    parent = session_manager.register(
        external_id=f"parent-{run_id}",
        machine_id="machine-1",
        source="claude",
        project_id=sample_project["id"],
    )
    child = session_manager.register(
        external_id=f"child-{run_id}",
        machine_id="machine-1",
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
        tmux_session_name=f"gobby-{run_id[-4:]}",
    )
    return monitor, run


@pytest.mark.asyncio
async def test_read_codex_transcript_snapshot_keeps_redacted_tail(tmp_path: Path) -> None:
    transcript_path = tmp_path / "rollout.jsonl"
    _write_codex_transcript(transcript_path)

    snapshot = await IdleCheckHandler._read_codex_transcript_snapshot(str(transcript_path), limit=1)

    assert len(snapshot.response_items) == 1
    assert snapshot.response_items[0].payload_type == "custom_tool_call"
    assert snapshot.response_items[0].event_type == "response_item"
    assert "apply_patch" not in json.dumps(snapshot.to_log_dict())


@pytest.mark.asyncio
async def test_read_codex_transcript_snapshot_tracks_latest_lifecycle_event(
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "rollout-lifecycle.jsonl"
    _write_codex_lifecycle_transcript(
        transcript_path,
        lifecycle_events=("task_started", "task_complete", "task_started"),
    )

    snapshot = await IdleCheckHandler._read_codex_transcript_snapshot(str(transcript_path))

    assert snapshot.lifecycle_event is not None
    assert snapshot.lifecycle_event.line_num == 3
    assert snapshot.lifecycle_event.event_type == "event_msg"
    assert snapshot.lifecycle_event.payload_type == "task_started"
    assert snapshot.has_conclusive_task_complete is False
    assert "prompt-and-tool-secret" not in json.dumps(snapshot.to_log_dict())


@pytest.mark.asyncio
async def test_read_codex_transcript_snapshot_confirms_redacted_capacity_error(
    tmp_path: Path,
) -> None:
    transcript_path = tmp_path / "codex-capacity.jsonl"
    _append_codex_capacity_turn(transcript_path)

    snapshot = await IdleCheckHandler._read_codex_transcript_snapshot(str(transcript_path))

    assert snapshot.has_conclusive_capacity_error is True
    assert snapshot.capacity_error_event is not None
    assert snapshot.capacity_error_event.payload_type == "error"
    assert snapshot.latest_model_output_line_num is None
    assert _CAPACITY_MESSAGE not in json.dumps(snapshot.to_log_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "error_info", "lifecycle_suffix", "malformed_tail"),
    [
        ("A different provider error", "server_overloaded", (), False),
        (_CAPACITY_MESSAGE, "other_error", (), False),
        (_CAPACITY_MESSAGE, "server_overloaded", ("task_started", "task_complete"), False),
        (_CAPACITY_MESSAGE, "server_overloaded", (), True),
    ],
    ids=["wrong-message", "wrong-error-info", "prior-turn", "malformed-tail"],
)
async def test_read_codex_transcript_snapshot_rejects_inconclusive_capacity_error(
    tmp_path: Path,
    message: str,
    error_info: str,
    lifecycle_suffix: tuple[str, ...],
    malformed_tail: bool,
) -> None:
    transcript_path = tmp_path / "codex-inconclusive-capacity.jsonl"
    _append_codex_capacity_turn(
        transcript_path,
        message=message,
        error_info=error_info,
        lifecycle_suffix=lifecycle_suffix,
        malformed_tail=malformed_tail,
    )

    snapshot = await IdleCheckHandler._read_codex_transcript_snapshot(str(transcript_path))

    assert snapshot.has_conclusive_capacity_error is False


@pytest.mark.asyncio
async def test_task_complete_reprompts_after_base_timeout_before_semantic_delay(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
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
            monitor._idle_check_handler,
            "_idle_reprompt_message",
            new_callable=AsyncMock,
            return_value="workflow-aware continuation",
        ) as mock_message,
        patch.object(
            monitor._idle_check_handler,
            "_record_watchdog_task_event",
            new_callable=AsyncMock,
        ) as mock_audit,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    mock_message.assert_awaited_once()
    mock_send.assert_has_awaits(
        [
            call(run.tmux_session_name, "Escape", literal=False),
            call(run.tmux_session_name, "workflow-aware continuation"),
            call(run.tmux_session_name, "Enter", literal=False),
        ]
    )
    assert all(awaited.args[1] != "C-c" for awaited in mock_send.await_args_list)
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 1
    mock_audit.assert_awaited_once_with(
        run,
        action="task_complete_reprompt",
        session_id=run.child_session_id,
        detail="latest_lifecycle_event=task_complete",
    )


@pytest.mark.asyncio
async def test_fresh_task_complete_waits_for_base_timeout_before_any_recovery(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
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
    sample_project: dict,
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
            monitor._idle_check_handler,
            "_idle_reprompt_message",
            new_callable=AsyncMock,
            return_value="workflow-aware continuation",
        ),
        patch.object(
            monitor._idle_check_handler,
            "_record_watchdog_task_event",
            new_callable=AsyncMock,
        ) as mock_audit,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 1
    state = monitor._idle_check_handler._codex_capacity_recovery[run.id]
    assert state.successful_reprompts == 1
    mock_send.assert_has_awaits(
        [
            call(run.tmux_session_name, "Escape", literal=False),
            call(run.tmux_session_name, "workflow-aware continuation"),
            call(run.tmux_session_name, "Enter", literal=False),
        ]
    )
    mock_audit.assert_awaited_once_with(
        run,
        action="capacity_reprompt",
        session_id=run.child_session_id,
        detail="codex_error_info=server_overloaded;attempt=1/2",
    )


@pytest.mark.asyncio
async def test_capacity_pane_text_requires_structured_transcript_confirmation(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
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
    sample_project: dict,
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
            monitor._idle_check_handler,
            "_idle_reprompt_message",
            new_callable=AsyncMock,
            return_value="continue",
        ),
        patch.object(
            monitor._idle_check_handler,
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
    state = monitor._idle_check_handler._codex_capacity_recovery[run.id]
    assert state.successful_reprompts == 1


@pytest.mark.asyncio
async def test_capacity_reprompts_are_bounded_across_user_only_retry_turns(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
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
            monitor._idle_check_handler,
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
    sample_project: dict,
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
            monitor._idle_check_handler,
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
    state = monitor._idle_check_handler._codex_capacity_recovery[run.id]
    assert state.successful_reprompts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "child_source", "lifecycle_events", "malformed_tail"),
    [
        ("later-start", "codex", ("task_complete", "task_started"), False),
        ("non-codex", "claude", ("task_complete",), False),
        ("malformed", "codex", ("task_complete",), True),
    ],
)
async def test_completed_turn_expedited_recovery_requires_conclusive_codex_marker(
    case: str,
    child_source: str,
    lifecycle_events: tuple[str, ...],
    malformed_tail: bool,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
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
            "non-codex": "dddddddd-dddd-4ddd-8ddd-dddddddd1012",
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
    sample_project: dict,
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
            monitor._idle_check_handler,
            "_idle_reprompt_message",
            new_callable=AsyncMock,
            return_value="delayed continuation",
        ),
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    mock_send.assert_has_awaits(
        [
            call(run.tmux_session_name, "Escape", literal=False),
            call(run.tmux_session_name, "delayed continuation"),
            call(run.tmux_session_name, "Enter", literal=False),
        ]
    )
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 1


@pytest.mark.asyncio
async def test_completed_turn_recovery_preserves_unsubmitted_input(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
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
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 0
    mock_send.assert_not_awaited()
    assert monitor._idle_detector.get_state(run.id).reprompt_count == 0


@pytest.mark.asyncio
async def test_recent_codex_session_activity_only_checks_capacity_pane(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
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
    mock_capture.assert_awaited_once_with(_run.tmux_session_name, lines=15)
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_turn_recovery_retains_max_attempt_failure(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
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
async def test_idle_reprompt_falls_back_when_step_context_lookup_fails(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
    agent_run_manager: LocalAgentRunManager,
) -> None:
    """Unexpected step-context lookup errors fall back and log exception context."""
    config = TmuxConfig(idle_check_enabled=True, idle_timeout_seconds=10, max_reprompt_attempts=2)
    monitor = AgentLifecycleMonitor(
        agent_run_manager=agent_run_manager,
        db=temp_db,
        session_manager=session_manager,
        check_interval_seconds=1.0,
        tmux_config=config,
    )
    parent = session_manager.register(
        external_id="parent-session-fallback",
        machine_id="machine-1",
        source="claude",
        project_id=sample_project["id"],
    )
    child = session_manager.register(
        external_id="codex-child-fallback",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    run = _make_terminal_run(
        agent_run_manager,
        parent.to_dict(),
        child_session_id=child.id,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1001",
        tmux_session_name="gobby-codex-fallback",
    )

    with (
        patch(
            "gobby.agents.idle_check_handler.get_active_step_workflow_context",
            side_effect=RuntimeError("context lookup failed"),
        ),
        patch("gobby.agents.idle_check_handler.logger.exception") as mock_exception,
    ):
        message = await monitor._idle_check_handler._idle_reprompt_message(run)

    assert message == IdleDetector.REPROMPT_MESSAGE
    mock_exception.assert_called_once()
    assert (
        "Unexpected error loading active step workflow context" in mock_exception.call_args.args[0]
    )


@pytest.mark.asyncio
async def test_idle_reprompt_logs_codex_response_items(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict,
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
    temp_db.execute("UPDATE sessions SET updated_at = %s WHERE id = %s", (stale_time, child.id))

    run = _make_terminal_run(
        agent_run_manager,
        parent.to_dict(),
        child_session_id=child.id,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1002",
        tmux_session_name="gobby-codex-idle",
    )
    state = monitor._idle_detector.get_state(run.id)
    state.first_idle_at = time.monotonic() - 360

    with (
        patch.object(monitor._tmux, "capture_pane", new_callable=AsyncMock, return_value="❯\n"),
        patch.object(monitor._tmux, "send_keys", new_callable=AsyncMock, return_value=True),
        patch("gobby.agents.idle_check_handler.logger.warning") as mock_warning,
    ):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    diagnostics = [
        str(log_call.args[4])
        for log_call in mock_warning.call_args_list
        if log_call.args
        and log_call.args[0] == "Codex idle diagnostic for run %s (%s) session %s: %s"
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
    temp_db.execute("UPDATE sessions SET updated_at = %s WHERE id = %s", (stale_time, child.id))

    run = _make_terminal_run(
        agent_run_manager,
        parent.to_dict(),
        child_session_id=child.id,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1003",
        tmux_session_name="gobby-codex-reasoning",
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
        patch("gobby.agents.idle_check_handler.logger.warning") as mock_warning,
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
        str(log_call.args[3])
        for log_call in mock_warning.call_args_list
        if log_call.args
        and log_call.args[0] == "Codex reasoning watchdog interrupting run %s session %s: %s"
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
    temp_db.execute("UPDATE sessions SET updated_at = %s WHERE id = %s", (stale_time, child.id))

    run = _make_terminal_run(
        agent_run_manager,
        parent.to_dict(),
        child_session_id=child.id,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd1004",
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

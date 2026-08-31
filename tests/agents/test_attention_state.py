"""Acceptance tests for persisted agent attention episodes."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.attention_tracker import AgentAttentionTracker
from gobby.agents.idle_check_handler import IdleCheckHandler
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.stall_classifier import StallClassifier
from gobby.agents.tmux.pane_monitor import TmuxPaneMonitor
from gobby.agents.watchdog import WatchdogReaderRegistry
from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager
from gobby.terminals.discovery import seed_external_terminal
from tests.agents.test_lifecycle_monitor import LifecycleRuntime
from tests.agents.test_lifecycle_monitor_extra import _memory_terminal_services
from tests.terminals.fakes import runtime_registry

from .detection_test_support import BundledDetectionRegistry

DETECTION_REGISTRY = BundledDetectionRegistry()
pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
APPROVAL_PANE = "Permission required: press Enter to approve this command"
PANE_CONTEXT: dict[str, Any] = {
    "tmux_socket_path": "/private/tmp/tmux-501/default",
    "tmux_pane": "%42",
    "tmux_session": "interactive",
    "tmux_window": "@1",
    "tmux_server_pid": 1658,
    "tmux_server_start_time": 1784592177,
}


def _agent_run() -> AgentRun:
    return AgentRun(
        id="run-1",
        parent_session_id="parent-1",
        child_session_id="session-1",
        provider="claude",
        prompt="test",
        status="running",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        terminal_id="agent-run-1",
    )


def _attention_manager(
    temp_db: HubDatabase,
    *,
    event_publisher: Any = None,
    notification_publisher: Any = None,
) -> Any:
    from gobby.storage.attention import AttentionStateManager

    return AttentionStateManager(
        temp_db,
        event_publisher=event_publisher,
        notification_publisher=notification_publisher,
        epoch="test-epoch",
    )


def _interactive_session(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> SimpleNamespace:
    """A registered session whose external pane is a live terminals row."""
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        row = session_manager.register(
            external_id="interactive-session",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=sample_project["id"],
        )
        seeded = seed_external_terminal(
            TerminalManager(session_manager.db),
            project_id=sample_project["id"],
            session_id=row.id,
            terminal_context=PANE_CONTEXT,
        )
    assert seeded is not None
    return SimpleNamespace(
        id=row.id, status="active", source="claude", terminal_context=PANE_CONTEXT
    )


def test_attention_migration_uses_entry_id_primary_key(temp_db: HubDatabase) -> None:
    columns = {
        row["column_name"]
        for row in temp_db.fetchall(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = 'attention_states'
            """
        )
    }
    primary_key = temp_db.fetchone(
        """
        SELECT COUNT(*) = 1 AND MIN(attribute.attname) = 'entry_id' AS valid
        FROM pg_constraint AS constraint_row
        JOIN unnest(constraint_row.conkey) WITH ORDINALITY AS key(attnum, ordinality)
          ON TRUE
        JOIN pg_attribute AS attribute
          ON attribute.attrelid = constraint_row.conrelid
         AND attribute.attnum = key.attnum
        WHERE constraint_row.conrelid = 'attention_states'::regclass
          AND constraint_row.contype = 'p'
        """
    )

    assert {
        "entry_id",
        "run_id",
        "session_id",
        "attention_id",
        "state",
        "reason",
        "kind",
        "fingerprint",
        "payload",
        "since",
        "seen_at",
        "updated_at",
    } <= columns
    assert primary_key is not None
    assert primary_key["valid"] is True


@pytest.mark.asyncio
async def test_blocked_transition_broadcasts_agent_event(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.runner_broadcasting import (
        _agent_broadcast_tasks,
        fire_agent_event,
        setup_agent_event_broadcasting,
    )

    pty_reader = MagicMock()
    tmux_reader = MagicMock()
    websocket = MagicMock()
    websocket.broadcast_agent_event = AsyncMock()
    monkeypatch.setattr("gobby.agents.pty_reader.get_pty_reader_manager", lambda: pty_reader)
    monkeypatch.setattr("gobby.agents.tmux.get_tmux_output_reader", lambda: tmux_reader)
    setup_agent_event_broadcasting(websocket)

    def publish(payload: dict[str, object]) -> None:
        fire_agent_event("attention_changed", "run-1", payload)

    manager = _attention_manager(temp_db, event_publisher=publish)
    transition = manager.transition(
        "run:run-1",
        state="blocked",
        run_id="run-1",
        session_id="session-1",
        reason="approval",
        kind="actionable",
        fingerprint="approval-v1",
        payload={"prompt_kind": "approval"},
    )
    await asyncio.gather(*tuple(_agent_broadcast_tasks))

    assert transition.applied is True
    websocket.broadcast_agent_event.assert_awaited_once()
    event = websocket.broadcast_agent_event.await_args.kwargs
    assert event["event"] == "attention_changed"
    assert event["entry_id"] == "run:run-1"
    assert event["attention_id"] == transition.current.attention_id
    assert event["state"] == "blocked"
    assert event["epoch"] == "test-epoch"
    assert event["seq"] == 1


def test_notification_dedupe_by_episode(temp_db: HubDatabase) -> None:
    notifications: list[dict[str, object]] = []
    manager = _attention_manager(temp_db, notification_publisher=notifications.append)

    first = manager.transition(
        "run:run-1",
        state="blocked",
        run_id="run-1",
        session_id="session-1",
        reason="approval",
        kind="actionable",
        fingerprint="same-prompt",
    )
    duplicate = manager.transition(
        "run:run-1",
        state="blocked",
        run_id="run-1",
        session_id="session-1",
        reason="approval",
        kind="actionable",
        fingerprint="same-prompt",
    )
    seen = manager.transition(
        "run:run-1",
        state="blocked",
        run_id="run-1",
        session_id="session-1",
        reason="approval",
        kind="actionable",
        fingerprint="same-prompt",
        mark_seen=True,
    )
    cleared = manager.transition(
        "run:run-1",
        state=None,
        expected_attention_id=seen.current.attention_id,
        expected_fingerprint="same-prompt",
    )
    second = manager.transition(
        "run:run-1",
        state="blocked",
        run_id="run-1",
        session_id="session-1",
        reason="approval",
        kind="actionable",
        fingerprint="same-prompt",
    )

    assert duplicate.applied is False
    assert seen.applied is True
    assert seen.current.attention_id == first.current.attention_id
    assert seen.current.seen_at is not None
    assert cleared.applied is True
    assert second.current.attention_id != first.current.attention_id
    assert [event["attention_id"] for event in notifications] == [
        first.current.attention_id,
        second.current.attention_id,
    ]


def test_stale_request_races(temp_db: HubDatabase) -> None:
    manager = _attention_manager(temp_db)
    first = manager.transition(
        "run:run-1",
        state="blocked",
        run_id="run-1",
        session_id="session-1",
        reason="approval",
        kind="actionable",
        fingerprint="approval-v1",
    )
    manager.transition(
        "run:run-1",
        state=None,
        expected_attention_id=first.current.attention_id,
        expected_fingerprint="approval-v1",
    )
    replacement = manager.transition(
        "run:run-1",
        state="blocked",
        run_id="run-1",
        session_id="session-1",
        reason="approval",
        kind="actionable",
        fingerprint="approval-v2",
    )

    stale_clear = manager.transition(
        "run:run-1",
        state=None,
        expected_attention_id=first.current.attention_id,
        expected_fingerprint="approval-v1",
    )

    assert stale_clear.applied is False
    assert stale_clear.current == replacement.current
    assert manager.get("run:run-1") == replacement.current


@pytest.mark.asyncio
async def test_attention_tracker_tracks_prompts_stalls_and_injection_clear(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _attention_manager(temp_db)
    prompt_detector = PromptDetector(DETECTION_REGISTRY, "claude")
    stall_classifier = StallClassifier(DETECTION_REGISTRY, "claude")

    async def run_db(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    config = MagicMock()
    config.auto_enter_approval_prompts = False
    tracker = AgentAttentionTracker(
        run_db=run_db,
        prompt_detector=prompt_detector,
        stall_classifier=stall_classifier,
        tmux_config=config,
        attention_manager=manager,
    )
    run = _agent_run()
    approval = "Permission required: press Enter to approve this command"

    await tracker.sync(run, approval)
    blocked = manager.get(f"run:{run.id}")
    assert blocked is not None
    assert blocked.state == "blocked"
    assert blocked.reason == "approval"
    assert blocked.payload["kind"] == "approval"
    assert blocked.payload["fingerprint"] == blocked.fingerprint

    await tracker.clear_after_injection(run)
    assert manager.get(f"run:{run.id}").state is None

    await tracker.sync(run, "Choose a response:\n1. Yes / 2. No\n")
    question = manager.get(f"run:{run.id}")
    assert question is not None
    assert question.reason == "question"
    assert question.payload["options"] == [
        {"option": 1, "label": "Yes"},
        {"option": 2, "label": "No"},
    ]
    await tracker.clear_after_injection(run)

    monkeypatch.setattr("gobby.agents.stall_classifier._MIN_CHECK_INTERVAL_SECONDS", 0)
    await tracker.sync(run, "503 service unavailable")
    await tracker.sync(run, "503 service unavailable")
    stalled = manager.get(f"run:{run.id}")
    assert stalled is not None
    assert stalled.state == "blocked"
    assert stalled.reason == "stall"
    assert stalled.kind == "non_actionable"
    assert stalled.payload["kind"] == "stall"
    assert stalled.payload["excerpt"] == "provider error in pane output"

    await tracker.sync(run, "503 service unavailable request-id=changed")
    repeated_stall = manager.get(f"run:{run.id}")
    assert repeated_stall.attention_id == stalled.attention_id
    assert repeated_stall.fingerprint == stalled.fingerprint
    assert repeated_stall.payload == stalled.payload

    await tracker.clear(run)
    assert manager.get(f"run:{run.id}").state is None


@pytest.mark.asyncio
async def test_idle_handler_checks_attention_without_waiting_for_idle(
    temp_db: HubDatabase,
) -> None:
    manager = _attention_manager(temp_db)
    run = _agent_run()
    run_manager = MagicMock()
    run_manager.list_active_for_machine.return_value = [run]
    services = _memory_terminal_services("agent-run-1")
    runtime = cast(LifecycleRuntime, services.registry.resolve("tmux"))
    runtime.snapshot_text = APPROVAL_PANE
    config = MagicMock()
    config.auto_enter_approval_prompts = False
    config.idle_timeout_seconds = 60

    async def run_db(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    handler = IdleCheckHandler(
        agent_run_manager=run_manager,
        db=temp_db,
        get_session_manager=lambda: None,
        tmux=MagicMock(),
        idle_detector=MagicMock(),
        prompt_detector=PromptDetector(DETECTION_REGISTRY, "claude"),
        stall_classifier=StallClassifier(DETECTION_REGISTRY, "claude"),
        watchdog_readers=WatchdogReaderRegistry(),
        cleanup_handler=MagicMock(),
        tmux_config=config,
        run_db=run_db,
        attention_manager=manager,
        terminal_services=services,
    )

    result = await handler.check_attention_agents()

    assert result == 1
    assert runtime.snapshot_calls == [15]
    attention = manager.get(f"run:{run.id}")
    assert attention is not None
    assert attention.state == "blocked"
    assert attention.reason == "approval"
    assert attention.payload["kind"] == "approval"


async def test_idle_check_reuses_attention_pane_and_stops_on_unknown(
    temp_db: HubDatabase,
) -> None:
    manager = _attention_manager(temp_db)
    run = _agent_run()
    run_manager = MagicMock()
    run_manager.list_active_for_machine.return_value = [run]
    run_manager.get.return_value = run
    services = _memory_terminal_services("agent-run-1")
    runtime = cast(LifecycleRuntime, services.registry.resolve("tmux"))
    runtime.snapshot_text = APPROVAL_PANE
    config = MagicMock()
    config.auto_enter_approval_prompts = False
    config.idle_check_enabled = True
    config.idle_timeout_seconds = 60

    bound_idle_detector = MagicMock()
    bound_idle_detector.detect.return_value = "unknown"
    idle_detector = MagicMock()
    idle_detector.for_provider.return_value = bound_idle_detector

    async def run_db(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    handler = IdleCheckHandler(
        agent_run_manager=run_manager,
        db=temp_db,
        get_session_manager=lambda: None,
        tmux=MagicMock(),
        idle_detector=idle_detector,
        prompt_detector=PromptDetector(DETECTION_REGISTRY, "claude"),
        stall_classifier=StallClassifier(DETECTION_REGISTRY, "claude"),
        watchdog_readers=WatchdogReaderRegistry(),
        cleanup_handler=MagicMock(),
        tmux_config=config,
        run_db=run_db,
        attention_manager=manager,
        terminal_services=services,
    )

    with patch.object(
        handler._attention_tracker,
        "sync",
        wraps=handler._attention_tracker.sync,
    ) as sync_attention:
        await handler.check_attention_agents(reuse_for_idle=True)
        result = await handler.check_idle_agents()

    assert result == 0
    attention = manager.get(f"run:{run.id}")
    assert attention is not None
    assert attention.state == "blocked"
    assert attention.reason == "approval"
    assert runtime.snapshot_calls == [15]
    sync_attention.assert_awaited_once()
    bound_idle_detector.reset_idle.assert_called_once_with(run.id)
    bound_idle_detector.has_unsubmitted_input.assert_not_called()
    bound_idle_detector.should_fail.assert_not_called()


@pytest.mark.asyncio
async def test_tmux_monitor_reports_interactive_prompt_without_injection(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    manager = _attention_manager(temp_db)
    session = _interactive_session(session_manager, sample_project)
    sessions = MagicMock()
    sessions.db = temp_db
    sessions.get.side_effect = AssertionError("provider must come from listed session")
    sessions.list.return_value = [session]
    runtime = LifecycleRuntime(snapshot_text=APPROVAL_PANE)
    monitor = TmuxPaneMonitor(
        detection_registry=DETECTION_REGISTRY,
        session_end_callback=MagicMock(),
        session_manager=sessions,
        attention_manager=manager,
        prompt_detector=PromptDetector(DETECTION_REGISTRY, "claude"),
        stall_classifier=StallClassifier(DETECTION_REGISTRY, "claude"),
        registry=runtime_registry(runtime),
    )

    await monitor._check_attention_panes(active_runs=[])

    attention = manager.get(f"session:{session.id}")
    assert attention is not None
    assert attention.state == "blocked"
    assert attention.reason == "approval"
    assert attention.payload["kind"] == "approval"
    assert runtime.write_log == []


@pytest.mark.asyncio
async def test_tmux_monitor_keeps_attention_on_capture_timeout_and_recovers(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = _attention_manager(temp_db)
    session = _interactive_session(session_manager, sample_project)
    sessions = MagicMock()
    sessions.db = temp_db
    sessions.list.return_value = [session]
    runtime = LifecycleRuntime(snapshot_text=APPROVAL_PANE)
    monitor = TmuxPaneMonitor(
        detection_registry=DETECTION_REGISTRY,
        session_end_callback=MagicMock(),
        session_manager=sessions,
        attention_manager=manager,
        prompt_detector=PromptDetector(DETECTION_REGISTRY, "claude"),
        stall_classifier=StallClassifier(DETECTION_REGISTRY, "claude"),
        registry=runtime_registry(runtime),
    )
    await monitor._check_attention_panes(active_runs=[])
    before_timeout = manager.get(f"session:{session.id}")
    assert before_timeout is not None

    runtime.snapshot_calls.clear()
    runtime.snapshot_error = TimeoutError("tmux command timed out")
    caplog.clear()
    logger_name = "gobby.agents.tmux.pane_monitor"
    with caplog.at_level(logging.DEBUG, logger=logger_name):
        await monitor._check_attention_panes(active_runs=[])

    after_timeout = manager.get(f"session:{session.id}")
    assert after_timeout is not None
    assert after_timeout.attention_id == before_timeout.attention_id
    assert after_timeout.fingerprint == before_timeout.fingerprint
    assert after_timeout.state == "blocked"
    assert runtime.snapshot_calls == [15]
    records = [record for record in caplog.records if record.name == logger_name]
    assert not [record for record in records if record.levelno >= logging.WARNING]
    timeout_record = next(
        record for record in records if record.getMessage().endswith("pane capture timed out")
    )
    assert timeout_record.exc_info is None
    assert timeout_record.__dict__["pane_id"] == "%42"
    assert timeout_record.__dict__["session_id"] == session.id
    assert timeout_record.__dict__["provider"] == "claude"

    runtime.snapshot_error = None
    runtime.snapshot_text = "Working normally"
    await monitor._check_attention_panes(active_runs=[])

    recovered = manager.get(f"session:{session.id}")
    assert recovered is not None
    assert recovered.state is None

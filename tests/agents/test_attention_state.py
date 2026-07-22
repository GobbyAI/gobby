"""Acceptance tests for persisted agent attention episodes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.idle_check_handler import IdleCheckHandler
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.stall_classifier import StallClassifier
from gobby.agents.tmux.pane_monitor import TmuxPaneMonitor
from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


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
        tmux_session_name="agent-run-1",
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
async def test_idle_handler_tracks_prompts_stalls_and_injection_clear(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _attention_manager(temp_db)
    prompt_detector = PromptDetector()
    stall_classifier = StallClassifier()

    async def run_db(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    config = MagicMock()
    config.auto_enter_approval_prompts = False
    handler = IdleCheckHandler(
        agent_run_manager=MagicMock(),
        db=temp_db,
        get_session_manager=lambda: None,
        tmux=MagicMock(),
        idle_detector=MagicMock(),
        cleanup_handler=MagicMock(),
        tmux_config=config,
        run_db=run_db,
        attention_manager=manager,
        prompt_detector=prompt_detector,
        stall_classifier=stall_classifier,
    )
    run = _agent_run()
    approval = "Permission required: press Enter to approve this command"

    await handler.sync_attention(run, approval)
    blocked = manager.get(f"run:{run.id}")
    assert blocked is not None
    assert blocked.state == "blocked"
    assert blocked.reason == "approval"

    await handler.clear_attention_after_injection(run)
    assert manager.get(f"run:{run.id}").state is None

    monkeypatch.setattr("gobby.agents.stall_classifier._MIN_CHECK_INTERVAL_SECONDS", 0)
    await handler.sync_attention(run, "503 service unavailable")
    await handler.sync_attention(run, "503 service unavailable")
    stalled = manager.get(f"run:{run.id}")
    assert stalled is not None
    assert stalled.state == "blocked"
    assert stalled.reason == "stall"
    assert stalled.kind == "non_actionable"

    await handler.clear_attention(run)
    assert manager.get(f"run:{run.id}").state is None


@pytest.mark.asyncio
async def test_idle_handler_checks_attention_without_waiting_for_idle(
    temp_db: HubDatabase,
) -> None:
    manager = _attention_manager(temp_db)
    run = _agent_run()
    run_manager = MagicMock()
    run_manager.list_active.return_value = [run]
    tmux = MagicMock()
    tmux.capture_pane = AsyncMock(
        return_value="Permission required: press Enter to approve this command"
    )
    config = MagicMock()
    config.auto_enter_approval_prompts = False
    config.idle_timeout_seconds = 60

    async def run_db(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    handler = IdleCheckHandler(
        agent_run_manager=run_manager,
        db=temp_db,
        get_session_manager=lambda: None,
        tmux=tmux,
        idle_detector=MagicMock(),
        cleanup_handler=MagicMock(),
        tmux_config=config,
        run_db=run_db,
        attention_manager=manager,
    )

    result = await handler.check_attention_agents()

    assert result == 1
    attention = manager.get(f"run:{run.id}")
    assert attention is not None
    assert attention.state == "blocked"
    assert attention.reason == "approval"


@pytest.mark.asyncio
async def test_tmux_monitor_reports_interactive_prompt_without_injection(
    temp_db: HubDatabase,
) -> None:
    manager = _attention_manager(temp_db)
    session = SimpleNamespace(
        id="interactive-session",
        status="active",
        terminal_context={"tmux_pane": "%42"},
    )
    session_manager = MagicMock()
    session_manager.db = temp_db
    session_manager.list.return_value = [session]
    tmux = MagicMock()
    tmux.capture_pane = AsyncMock(
        return_value="Permission required: press Enter to approve this command"
    )
    tmux.send_keys = AsyncMock()
    monitor = TmuxPaneMonitor(
        session_end_callback=MagicMock(),
        session_manager=session_manager,
        attention_manager=manager,
        prompt_detector=PromptDetector(),
        stall_classifier=StallClassifier(),
        tmux_manager_factory=lambda _context: tmux,
    )

    await monitor._check_attention_panes(active_runs=[])

    attention = manager.get(f"session:{session.id}")
    assert attention is not None
    assert attention.state == "blocked"
    assert attention.reason == "approval"
    tmux.send_keys.assert_not_awaited()

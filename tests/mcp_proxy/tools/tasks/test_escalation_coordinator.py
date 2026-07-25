"""Tests for authoritative escalation event coordination."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import gobby.storage.tasks._transitions as transitions
from gobby.mcp_proxy.tools.tasks._escalation_coordinator import (
    coordinate_task_escalation,
    derive_escalation_event_id,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager


def _context(db: HubDatabase, manager: LocalTaskManager) -> SimpleNamespace:
    return SimpleNamespace(
        task_manager=manager,
        session_task_manager=SessionTaskManager(db),
        session_var_manager=MagicMock(),
        resolve_session_id=lambda session_id: session_id,
    )


def _session(session_manager: SessionManager, project_id: str) -> Any:
    return session_manager.register(
        external_id="validation-exit-ramp-session",
        machine_id="test-machine",
        source="codex",
        project_id=project_id,
    )


def test_event_id_is_stable_then_fresh_after_re_escalation(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Escalation identity",
        validation_criteria="Test task completion is observable.",
    )
    first_at = datetime(2026, 1, 1, tzinfo=UTC)
    second_at = first_at + timedelta(seconds=1)
    times = iter((first_at, second_at))
    monkeypatch.setattr(transitions, "utc_now", lambda: next(times))

    first = manager.escalate_task(task.id, reason="first")
    first_id = derive_escalation_event_id(first.id, first.escalated_at)
    reread = manager.get_task(task.id)
    assert derive_escalation_event_id(reread.id, reread.escalated_at) == first_id

    manager.de_escalate_task(task.id, reason="resolved")
    second = manager.escalate_task(task.id, reason="second")

    assert derive_escalation_event_id(second.id, second.escalated_at) != first_id


def test_coordinator_upserts_session_link_and_attaches_event_id(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Escalation delivery",
        validation_criteria="Test task completion is observable.",
    )
    escalated = manager.escalate_task(task.id, reason="threshold")
    session = _session(session_manager, sample_project["id"])
    ctx = _context(temp_db, manager)
    notify = MagicMock()
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._escalation_coordinator.notify_parent_on_task_state_change",
        notify,
    )

    first_id = coordinate_task_escalation(
        ctx,
        escalated,
        prior_owner_session_id=None,
        session_id=session.id,
    )
    second_id = coordinate_task_escalation(
        ctx,
        manager.get_task(task.id),
        prior_owner_session_id=None,
        session_id=session.id,
    )

    assert first_id == second_id
    assert notify.call_args.kwargs["event_id"] == first_id
    row = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM session_tasks "
        "WHERE session_id = %s AND task_id = %s AND action = 'escalated'",
        (session.id, task.id),
    )
    assert row is not None and row["count"] == 1


def test_notification_failure_does_not_change_authoritative_escalation(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Best effort notification",
        validation_criteria="Test task completion is observable.",
    )
    escalated = manager.escalate_task(task.id, reason="threshold")
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._escalation_coordinator.notify_parent_on_task_state_change",
        MagicMock(side_effect=RuntimeError("websocket unavailable")),
    )

    event_id = coordinate_task_escalation(
        _context(temp_db, manager),
        escalated,
        prior_owner_session_id=None,
        session_id=None,
    )

    authoritative = manager.get_task(task.id)
    assert authoritative.is_escalated is True
    assert authoritative.escalated_at == escalated.escalated_at
    assert event_id == derive_escalation_event_id(task.id, authoritative.escalated_at)

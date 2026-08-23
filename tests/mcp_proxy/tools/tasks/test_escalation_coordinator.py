"""Tests for authoritative escalation event coordination."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

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

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


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
        machine_id="21000000-0000-4000-8000-000000000002",
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


class _RecordingSessionVariables:
    """Minimal session-variable manager that applies merges in memory."""

    def __init__(self, variables: dict[str, Any]) -> None:
        self.variables = variables

    def get_variables(self, _session_id: str) -> dict[str, Any]:
        return self.variables

    def merge_variables(self, _session_id: str, merge: dict[str, Any]) -> None:
        self.variables.update(merge)


def test_escalation_releases_the_claim_but_keeps_edit_attribution(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B.3: escalation is a pause, so the escalating session stays the files' owner.

    Dropping the attribution here blinds close gates 7, 9, 10 and 12 for a task that
    really did edit files, and leaves uncommitted work in a shared worktree with no
    resolvable owner.
    """
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Escalation keeps attribution",
        validation_criteria="Test task completion is observable.",
    )
    escalated = manager.escalate_task(task.id, reason="blocked on a decision")
    session_vars = _RecordingSessionVariables(
        {
            "active_task_id": task.id,
            "claimed_tasks": {task.id: f"#{task.seq_num}"},
            "task_edited_files": {task.id: ["src/gobby/memory/recall.py"]},
            "task_edited_file_checkouts": {task.id: {"/repo": ["src/gobby/memory/recall.py"]}},
        }
    )
    ctx = _context(temp_db, manager)
    ctx.session_var_manager = session_vars
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._escalation_coordinator.notify_parent_on_task_state_change",
        MagicMock(),
    )

    coordinate_task_escalation(
        ctx,
        escalated,
        prior_owner_session_id="owner-session",
        session_id=None,
    )

    assert session_vars.variables["claimed_tasks"] == {}
    assert session_vars.variables["task_claimed"] is False
    assert session_vars.variables["active_task_id"] is None
    assert session_vars.variables["task_edited_files"] == {task.id: ["src/gobby/memory/recall.py"]}
    assert session_vars.variables["task_edited_file_checkouts"] == {
        task.id: {"/repo": ["src/gobby/memory/recall.py"]}
    }


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
    assert authoritative.escalated_at is not None
    assert authoritative.escalated_at == escalated.escalated_at
    assert event_id == derive_escalation_event_id(task.id, authoritative.escalated_at)

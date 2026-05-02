"""Tests for hook session lookup metadata preservation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.session_lookup import SessionLookupService

pytestmark = pytest.mark.unit


def _event(metadata: dict | None = None) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="claude-external",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"terminal_context": {"tmux_pane": "%1"}},
        metadata=metadata or {},
    )


def _service(
    session_manager: MagicMock,
    session_task_manager: MagicMock,
    resolve_project_id: MagicMock,
) -> SessionLookupService:
    coordinator = MagicMock()
    return SessionLookupService(
        session_manager=session_manager,
        session_coordinator=coordinator,
        session_task_manager=session_task_manager,
        get_machine_id=lambda: "machine-id",
        resolve_project_id=resolve_project_id,
        logger=MagicMock(),
    )


def test_valid_platform_session_metadata_is_preserved_and_enriched() -> None:
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(
        id="platform-session", project_id="project-1"
    )
    session_manager.backfill_terminal_context.return_value = (None, False)
    session_task_manager = MagicMock()
    session_task_manager.get_session_tasks.return_value = []
    resolve_project_id = MagicMock(return_value="wrong-project")
    service = _service(session_manager, session_task_manager, resolve_project_id)
    event = _event({"_platform_session_id": "platform-session"})

    result = service.resolve(event)

    assert result == "platform-session"
    assert event.project_id == "project-1"
    assert event.metadata["_platform_session_id"] == "platform-session"
    session_manager.get_session_id.assert_not_called()
    resolve_project_id.assert_not_called()
    session_manager.backfill_terminal_context.assert_called_once_with(
        "platform-session",
        {"tmux_pane": "%1"},
    )
    session_task_manager.get_session_tasks.assert_called_once_with("platform-session")


def test_invalid_platform_session_metadata_falls_back_to_external_lookup() -> None:
    session_manager = MagicMock()
    session_manager.get.return_value = None
    session_manager.get_session_id.return_value = "mapped-platform-session"
    session_manager.backfill_terminal_context.return_value = (None, False)
    session_task_manager = MagicMock()
    session_task_manager.get_session_tasks.return_value = []
    resolve_project_id = MagicMock(return_value="project-from-cwd")
    service = _service(session_manager, session_task_manager, resolve_project_id)
    event = _event({"_platform_session_id": "missing-platform-session"})

    result = service.resolve(event)

    assert result == "mapped-platform-session"
    assert event.project_id == "project-from-cwd"
    assert event.metadata["_platform_session_id"] == "mapped-platform-session"
    session_manager.get_session_id.assert_called_once_with("claude-external", "claude")


def test_task_context_uses_stage_native_status() -> None:
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(
        id="platform-session", project_id="project-1"
    )
    session_manager.backfill_terminal_context.return_value = (None, False)
    session_task_manager = MagicMock()
    task = SimpleNamespace(
        id="task-1",
        title="Stage-native task",
        stages=[SimpleNamespace(name="implementation", state="in_progress", position=1)],
        closed_at=None,
        is_escalated=False,
        active_blocked_by=[],
    )
    session_task_manager.get_session_tasks.return_value = [{"task": task, "action": "worked_on"}]
    service = _service(session_manager, session_task_manager, MagicMock(return_value="project-1"))
    event = _event({"_platform_session_id": "platform-session"})

    service.resolve(event)

    assert event.task_id == "task-1"
    assert event.metadata["_task_title"] == "Stage-native task"
    assert event.metadata["_task_context"] == {
        "id": "task-1",
        "title": "Stage-native task",
        "status": "in_progress",
    }

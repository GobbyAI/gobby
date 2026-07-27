"""Tests for hook session lookup metadata preservation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.session_lookup import SessionLookupService

pytestmark = pytest.mark.unit


def _event(metadata: dict[str, Any] | None = None) -> HookEvent:
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


def test_terminal_context_backfill_adds_cwd_and_renames_empty_title() -> None:
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(
        id="platform-session", project_id="project-1"
    )
    updated_session = SimpleNamespace(
        id="platform-session",
        project_id="project-1",
        title=None,
        terminal_context={"tmux_pane": "%1", "cwd": "/work/repos/gobby"},
    )
    session_manager.backfill_terminal_context.return_value = (updated_session, True)
    session_task_manager = MagicMock()
    session_task_manager.get_session_tasks.return_value = []
    service = _service(session_manager, session_task_manager, MagicMock(return_value="project-1"))
    event = _event({"_platform_session_id": "platform-session"})
    event.data["cwd"] = "/work/repos/gobby"

    with patch("gobby.hooks.session_lookup.schedule_tmux_window_rename") as mock_schedule:
        result = service.resolve(event)

    assert result == "platform-session"
    session_manager.backfill_terminal_context.assert_called_once_with(
        "platform-session",
        {"tmux_pane": "%1", "cwd": "/work/repos/gobby"},
    )
    mock_schedule.assert_called_once()
    assert mock_schedule.call_args.args == (updated_session, "")


def test_root_cwd_platform_session_metadata_sets_project_on_event_data() -> None:
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
    event.source = SessionSource.CODEX
    event.cwd = "/"
    event.data["cwd"] = "/"

    result = service.resolve(event)

    assert result == "platform-session"
    assert event.project_id == "project-1"
    assert event.data["project_id"] == "project-1"
    resolve_project_id.assert_not_called()


def test_root_cwd_terminal_context_session_sets_project_before_lookup() -> None:
    session_manager = MagicMock()
    terminal_session = SimpleNamespace(id="terminal-session", project_id="project-1")
    session_manager.get.return_value = terminal_session
    session_manager.get_session_id.return_value = "mapped-platform-session"
    session_manager.backfill_terminal_context.return_value = (None, False)
    session_task_manager = MagicMock()
    session_task_manager.get_session_tasks.return_value = []
    resolve_project_id = MagicMock(return_value="wrong-project")
    service = _service(session_manager, session_task_manager, resolve_project_id)
    event = _event()
    event.source = SessionSource.CODEX
    event.cwd = "/"
    event.data["cwd"] = "/"
    event.data["terminal_context"]["gobby_session_id"] = "terminal-session"

    result = service.resolve(event)

    assert result == "mapped-platform-session"
    assert event.project_id == "project-1"
    assert event.data["project_id"] == "project-1"
    resolve_project_id.assert_not_called()
    session_manager.lookup_session_id.assert_not_called()


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
    session_manager.get_session_id.assert_any_call(
        "claude-external",
        "claude",
        machine_id="machine-id",
        project_id="project-from-cwd",
    )


def test_user_prompt_submit_weak_context_recovers_tmux_session_without_registering() -> None:
    recovered_session = SimpleNamespace(
        id="tmux-capable-session",
        project_id="project-1",
        source="claude",
        title="Existing terminal",
    )
    session_manager = MagicMock()
    session_manager.get_session_id.return_value = None
    session_manager.lookup_session_id.return_value = None
    session_manager.recover_session.return_value = recovered_session
    session_manager.backfill_terminal_context.return_value = (recovered_session, False)
    session_task_manager = MagicMock()
    session_task_manager.get_session_tasks.return_value = []
    resolve_project_id = MagicMock(return_value="project-1")
    service = _service(session_manager, session_task_manager, resolve_project_id)
    event = _event()
    event.event_type = HookEventType.BEFORE_AGENT
    event.session_id = "codex-external"
    event.source = SessionSource.CODEX
    event.cwd = "/work/repos/gobby"
    event.data = {
        "cwd": "/work/repos/gobby",
        "terminal_context": {"cwd": "/work/repos/gobby"},
    }

    result = service.resolve(event)

    assert result == "tmux-capable-session"
    assert event.metadata["_platform_session_id"] == "tmux-capable-session"
    session_manager.recover_session.assert_any_call(
        external_id="codex-external",
        source="codex",
        machine_id="machine-id",
        project_id="project-1",
    )
    session_manager.register_session.assert_not_called()
    session_manager.backfill_terminal_context.assert_called_once_with(
        "tmux-capable-session",
        {"cwd": "/work/repos/gobby"},
    )


def test_task_context_uses_stage_native_state() -> None:
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
    assert event.metadata["_task_id_origin"] == "session_context"
    assert event.metadata["_task_title"] == "Stage-native task"
    assert event.metadata["_task_context"] == {
        "id": "task-1",
        "title": "Stage-native task",
        "state": "in_progress",
    }


def test_task_context_preserves_explicit_task_and_enriches_matching_link() -> None:
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(
        id="platform-session", project_id="project-1"
    )
    session_manager.backfill_terminal_context.return_value = (None, False)
    latest_task = SimpleNamespace(
        id="task-latest",
        title="Latest session task",
        stages=[],
        closed_at=None,
        is_escalated=False,
        active_blocked_by=[],
    )
    explicit_task = SimpleNamespace(
        id="task-explicit",
        title="Explicit event task",
        stages=[],
        closed_at=None,
        is_escalated=False,
        active_blocked_by=[],
    )
    session_task_manager = MagicMock()
    session_task_manager.get_session_tasks.return_value = [
        {"task": latest_task, "action": "worked_on"},
        {"task": explicit_task, "action": "worked_on"},
    ]
    service = _service(session_manager, session_task_manager, MagicMock(return_value="project-1"))
    event = _event({"_platform_session_id": "platform-session"})
    event.task_id = explicit_task.id

    service.resolve(event)

    assert event.task_id == "task-explicit"
    assert event.metadata["_task_id_origin"] == "explicit"
    assert event.metadata["_task_title"] == "Explicit event task"
    assert event.metadata["_task_context"] == {
        "id": "task-explicit",
        "title": "Explicit event task",
        "state": "ready",
    }

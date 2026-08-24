"""Tests for hook session lookup metadata preservation."""

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.session_coordinator import SessionCoordinator
from gobby.hooks.session_lookup import SessionLookupService
from gobby.hooks.session_types import HookSessionManager
from gobby.sessions.compact_identity import CompactIdentityResolution
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_activity import SessionActivityResolution
from gobby.storage.session_models import Session
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

_REAL_MACHINE_ID = "21000000-0000-4000-8000-000000000009"


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
    logger: MagicMock | None = None,
) -> SessionLookupService:
    coordinator = MagicMock()
    return SessionLookupService(
        session_manager=session_manager,
        session_coordinator=coordinator,
        session_task_manager=session_task_manager,
        get_machine_id=lambda: "21000000-0000-4000-8000-000000000009",
        resolve_project_id=resolve_project_id,
        logger=logger or MagicMock(),
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
        project_id="project-from-cwd",
    )


def test_user_prompt_submit_weak_context_recovers_tmux_session_without_registering() -> None:
    recovered_session = SimpleNamespace(
        id="tmux-capable-session",
        project_id="project-1",
        source="claude",
        status="active",
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
        project_id="project-1",
    )
    session_manager.register_session.assert_not_called()
    session_manager.backfill_terminal_context.assert_called_once_with(
        "tmux-capable-session",
        {"cwd": "/work/repos/gobby"},
    )


@patch("gobby.hooks.session_lookup.reconcile_compact_session_activity")
@patch("gobby.hooks.session_lookup.resolve_compact_continuation")
def test_prestart_compact_traffic_recovers_canonical_row_without_registration(
    mock_resolve_compact: MagicMock,
    mock_reconcile: MagicMock,
) -> None:
    canonical = SimpleNamespace(
        id="canonical-session",
        external_id="canonical-provider-id",
        machine_id="21000000-0000-4000-8000-000000000009",
        project_id="project-1",
        source="claude",
        session_type="terminal",
        title="Canonical session",
    )
    canonical_session = cast(Session, canonical)
    mock_resolve_compact.return_value = CompactIdentityResolution(session=canonical_session)
    mock_reconcile.return_value = SessionActivityResolution(session=canonical_session)
    session_manager = MagicMock()
    session_manager.get_session_id.return_value = None
    session_manager.lookup_session_id.return_value = None
    session_manager.recover_session.return_value = None
    session_manager.backfill_terminal_context.return_value = (canonical, False)
    session_task_manager = MagicMock()
    session_task_manager.get_session_tasks.return_value = []
    service = _service(
        session_manager,
        session_task_manager,
        MagicMock(return_value="project-1"),
    )
    event = _event()
    event.data["terminal_context"] = {
        "tmux_pane": "%1",
        "parent_pid": 1234,
        "parent_create_time": 5678.0,
    }

    result = service.resolve(event)

    assert result == canonical.id
    assert event.session_id == canonical.external_id
    assert event.metadata["_observed_external_id"] == "claude-external"
    assert event.metadata["_platform_session_id"] == canonical.id
    session_manager.register_session.assert_not_called()


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


def _recovery_case(
    recovered: SimpleNamespace,
    logger: MagicMock,
) -> tuple[SessionLookupService, HookEvent]:
    """Drive resolve() down the recover_session branch with a watchable logger."""
    session_manager = MagicMock()
    session_manager.get_session_id.return_value = None
    session_manager.lookup_session_id.return_value = None
    session_manager.recover_session.return_value = recovered
    session_manager.backfill_terminal_context.return_value = (recovered, False)
    session_task_manager = MagicMock()
    session_task_manager.get_session_tasks.return_value = []
    resolve_project_id = MagicMock(return_value="project-1")
    service = _service(session_manager, session_task_manager, resolve_project_id, logger)
    event = _event()
    event.event_type = HookEventType.BEFORE_AGENT
    event.cwd = "/work/repos/gobby"
    event.data = {
        "cwd": "/work/repos/gobby",
        "terminal_context": {"cwd": "/work/repos/gobby"},
    }
    return service, event


def _recovery_records(logger_method: MagicMock) -> list[tuple[str, tuple[Any, ...]]]:
    return [
        (call.args[0], call.args[1:])
        for call in logger_method.call_args_list
        if "ecovered" in call.args[0]
    ]


def test_same_source_recovery_of_a_retired_row_names_status_not_source() -> None:
    logger = MagicMock()
    recovered = SimpleNamespace(
        id="retired-session",
        project_id="project-1",
        source="claude",
        status="expired",
        title="Retired terminal",
    )
    service, event = _recovery_case(recovered, logger)

    assert service.resolve(event) == "retired-session"

    # A WARNING here reaches errors.log, so a routine same-source recovery must
    # not emit one — that is the false positive this guards.
    assert _recovery_records(logger.warning) == []
    infos = _recovery_records(logger.info)
    assert len(infos) == 1
    template, args = infos[0]
    assert "source mismatch" not in template
    assert "expired" in (template % args)


def test_cross_source_recovery_still_warns_about_the_source() -> None:
    logger = MagicMock()
    recovered = SimpleNamespace(
        id="foreign-session",
        project_id="project-1",
        source="codex",
        status="active",
        title="Other CLI",
    )
    service, event = _recovery_case(recovered, logger)

    assert service.resolve(event) == "foreign-session"

    warnings = _recovery_records(logger.warning)
    assert len(warnings) == 1
    template, args = warnings[0]
    assert "source mismatch" in template
    rendered = template % args
    assert "incoming=claude" in rendered
    assert "existing=codex" in rendered


def test_same_source_recovery_of_a_live_row_does_not_claim_a_mismatch() -> None:
    logger = MagicMock()
    recovered = SimpleNamespace(
        id="live-session",
        project_id="project-1",
        source="claude",
        status="active",
        title="Live terminal",
    )
    service, event = _recovery_case(recovered, logger)

    assert service.resolve(event) == "live-session"

    assert _recovery_records(logger.warning) == []
    infos = _recovery_records(logger.info)
    assert len(infos) == 1
    template, _ = infos[0]
    assert "source mismatch" not in template


def test_expired_session_recovery_reports_status_through_the_real_path(
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End to end: a real expired row, the real service, the emitted record.

    The mocked tests above show what gets reported once the fallback is taken.
    This one proves what takes it, through real storage: the exact lookup skips
    the retired row, recovery returns it with the source matching exactly, and
    the service reports status at INFO instead of inventing a source mismatch.
    """
    with patch("gobby.utils.machine_id._cached_machine_id", _REAL_MACHINE_ID):
        project_id = (
            LocalProjectManager(temp_db)
            .create(
                name="lookup-project",
                repo_path="/tmp/lookup-project",
            )
            .id
        )
        storage_sessions = SessionManager(temp_db)
        # hook_manager.py casts at this same boundary: SessionManager serves the
        # HookSessionManager protocol at runtime without nominally declaring it.
        session_manager = cast(HookSessionManager, storage_sessions)
        terminal_context = {"tmux_pane": "%11", "tmux_socket_path": "/tmp/tmux-501/gobby"}
        registered = storage_sessions.register(
            external_id="retired-claude-session",
            machine_id=_REAL_MACHINE_ID,
            source="claude",
            project_id=project_id,
            terminal_context=terminal_context,
        )
        assert storage_sessions.mark_session_expired(registered.id)

        logger = logging.getLogger("tests.session_lookup.integration")
        service = SessionLookupService(
            session_manager=session_manager,
            session_coordinator=SessionCoordinator(session_storage=session_manager),
            session_task_manager=SessionTaskManager(temp_db),
            get_machine_id=lambda: _REAL_MACHINE_ID,
            resolve_project_id=lambda *_: project_id,
            logger=logger,
        )
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="retired-claude-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"terminal_context": dict(terminal_context)},
            metadata={},
        )

        with caplog.at_level(logging.INFO, logger=logger.name):
            resolved = service.resolve(event)

    assert resolved == registered.id
    recovery_records = [record for record in caplog.records if "ecovered" in record.getMessage()]
    assert len(recovery_records) == 1
    record = recovery_records[0]
    assert record.levelno == logging.INFO
    assert "source mismatch" not in record.getMessage()
    assert "expired" in record.getMessage()

"""Tests for hook session lookup metadata preservation."""

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from gobby.hooks.events import HookEvent, HookEventType, MissingHookMachineIdError, SessionSource
from gobby.hooks.session_coordinator import SessionCoordinator
from gobby.hooks.session_lookup import NON_MATERIALIZING_EVENTS, SessionLookupService
from gobby.hooks.session_types import HookSessionManager
from gobby.sessions.compact_identity import CompactIdentityResolution
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_activity import SessionActivityResolution
from gobby.storage.session_models import Session
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_deferred_startup_mark(request: pytest.FixtureRequest) -> Any:
    """No deferred startup left a mark: these mocked databases answer every query alike.

    Marks and their pins are covered against the real hub in test_session_startup_pins.
    """
    if "temp_db" in request.fixturenames:
        yield None
        return
    with patch("gobby.hooks.session_lookup.startup_marks.startup_mark", return_value=None) as mark:
        yield mark


_REAL_MACHINE_ID = "21000000-0000-4000-8000-000000000009"


def _event(
    metadata: dict[str, Any] | None = None,
    *,
    machine_id: str | None = _REAL_MACHINE_ID,
) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="claude-external",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"terminal_context": {"gobby_terminal_id": "1e557198-09a7-56ec-ba24-bb707c6faf68"}},
        metadata=metadata or {},
        machine_id=machine_id,
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
        resolve_project_id=resolve_project_id,
        logger=logger or MagicMock(),
    )


def test_session_lookup_requires_envelope_machine_id() -> None:
    session_manager = MagicMock()
    service = _service(session_manager, MagicMock(), MagicMock(return_value="project-1"))
    event = _event(machine_id=None)
    event.project_id = "project-1"

    with pytest.raises(
        MissingHookMachineIdError,
        match="Hook envelope is missing required machine_id",
    ):
        service.resolve(event)

    session_manager.get_session_id.assert_not_called()


def test_valid_platform_session_metadata_is_preserved_and_enriched() -> None:
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(
        id="platform-session", project_id="project-1", machine_id=_REAL_MACHINE_ID
    )
    session_manager.backfill_terminal_context.return_value = None
    session_task_manager = MagicMock()
    session_task_manager.get_worked_on_task.return_value = None
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
        {"gobby_terminal_id": "1e557198-09a7-56ec-ba24-bb707c6faf68"},
        session_manager.get.return_value,
    )
    session_manager.revive_expired_terminal_session.assert_called_once_with(
        "platform-session", None
    )
    session_task_manager.get_worked_on_task.assert_called_once_with("platform-session", None)


def test_terminal_context_backfill_adds_cwd() -> None:
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(
        id="platform-session", project_id="project-1", machine_id=_REAL_MACHINE_ID
    )
    updated_session = SimpleNamespace(
        id="platform-session",
        project_id="project-1",
        title=None,
        terminal_context={
            "gobby_terminal_id": "1e557198-09a7-56ec-ba24-bb707c6faf68",
            "cwd": "/work/repos/gobby",
        },
    )
    session_manager.backfill_terminal_context.return_value = updated_session
    session_task_manager = MagicMock()
    session_task_manager.get_worked_on_task.return_value = None
    service = _service(session_manager, session_task_manager, MagicMock(return_value="project-1"))
    event = _event({"_platform_session_id": "platform-session"})
    event.data["cwd"] = "/work/repos/gobby"

    result = service.resolve(event)

    assert result == "platform-session"
    session_manager.backfill_terminal_context.assert_called_once_with(
        "platform-session",
        {"gobby_terminal_id": "1e557198-09a7-56ec-ba24-bb707c6faf68", "cwd": "/work/repos/gobby"},
        session_manager.get.return_value,
    )
    session_manager.revive_expired_terminal_session.assert_called_once_with(
        "platform-session", updated_session
    )


def test_revival_judges_the_seat_recorded_from_this_hook() -> None:
    """A resumed CLI's new process is recorded before revival checks the seat."""
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(
        id="platform-session", project_id="project-1", machine_id=_REAL_MACHINE_ID
    )
    session_manager.backfill_terminal_context.return_value = None
    session_task_manager = MagicMock()
    session_task_manager.get_worked_on_task.return_value = None
    service = _service(session_manager, session_task_manager, MagicMock(return_value="project-1"))

    service.resolve(_event({"_platform_session_id": "platform-session"}))

    mutations = [
        name
        for name, _args, _kwargs in session_manager.mock_calls
        if name in {"backfill_terminal_context", "revive_expired_terminal_session"}
    ]
    assert mutations == ["backfill_terminal_context", "revive_expired_terminal_session"]


def test_root_cwd_platform_session_metadata_sets_project_on_event_data() -> None:
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(
        id="platform-session",
        project_id="project-1",
        machine_id=_REAL_MACHINE_ID,
        terminal_context=None,
        transcript_path=None,
    )
    session_manager.backfill_terminal_context.return_value = None
    session_task_manager = MagicMock()
    session_task_manager.get_worked_on_task.return_value = None
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
    terminal_session = SimpleNamespace(
        id="terminal-session",
        project_id="project-1",
        machine_id=_REAL_MACHINE_ID,
        terminal_context=None,
        transcript_path=None,
    )
    session_manager.get.return_value = terminal_session
    session_manager.get_session_id.return_value = "mapped-platform-session"
    session_manager.backfill_terminal_context.return_value = None
    session_task_manager = MagicMock()
    session_task_manager.get_worked_on_task.return_value = None
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
    mapped = SimpleNamespace(id="mapped-platform-session", machine_id=_REAL_MACHINE_ID)
    session_manager.get.side_effect = lambda ref: mapped if ref == mapped.id else None
    session_manager.get_session_id.return_value = "mapped-platform-session"
    session_manager.backfill_terminal_context.return_value = None
    session_task_manager = MagicMock()
    session_task_manager.get_worked_on_task.return_value = None
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


def test_user_prompt_submit_weak_context_recovers_terminal_session_without_registering() -> None:
    recovered_session = SimpleNamespace(
        id="terminal-capable-session",
        project_id="project-1",
        machine_id=_REAL_MACHINE_ID,
        source="claude",
        status="active",
        title="Existing terminal",
        terminal_context=None,
        transcript_path=None,
    )
    session_manager = MagicMock()
    session_manager.get_session_id.return_value = None
    session_manager.lookup_session_id.return_value = None
    session_manager.recover_session.return_value = recovered_session
    session_manager.get.return_value = recovered_session
    session_manager.backfill_terminal_context.return_value = recovered_session
    session_task_manager = MagicMock()
    session_task_manager.get_worked_on_task.return_value = None
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

    assert result == "terminal-capable-session"
    assert event.metadata["_platform_session_id"] == "terminal-capable-session"
    session_manager.recover_session.assert_any_call(
        external_id="codex-external",
        source="codex",
        project_id="project-1",
    )
    session_manager.register_session.assert_not_called()
    session_manager.backfill_terminal_context.assert_called_once_with(
        "terminal-capable-session",
        {"cwd": "/work/repos/gobby"},
        None,
    )
    session_manager.revive_expired_terminal_session.assert_called_once_with(
        "terminal-capable-session", recovered_session
    )


# Sorted: set order follows the per-process hash seed, and xdist workers must collect alike.
@pytest.mark.parametrize("event_type", sorted(NON_MATERIALIZING_EVENTS))
def test_passive_hooks_do_not_materialize_idle_session(event_type: HookEventType) -> None:
    session_manager = MagicMock()
    session_manager.get_session_id.return_value = None
    session_manager.lookup_session_id.return_value = None
    session_manager.recover_session.return_value = None
    session_task_manager = MagicMock()
    resolve_project_id = MagicMock(return_value="project-1")
    service = _service(session_manager, session_task_manager, resolve_project_id)
    event = _event()
    event.event_type = event_type

    result = service.resolve(event)

    assert result is None
    assert "_session_just_materialized" not in event.metadata
    session_manager.register_session.assert_not_called()


def test_resolve_uncached_marks_only_the_create_branch() -> None:
    session_manager = MagicMock()
    session_manager.get_session_id.return_value = None
    session_manager.lookup_session_id.return_value = "existing-session"
    session_manager.backfill_terminal_context.return_value = None
    session_manager.get.return_value.machine_id = _REAL_MACHINE_ID
    session_task_manager = MagicMock()
    session_task_manager.get_worked_on_task.return_value = None
    service = _service(
        session_manager,
        session_task_manager,
        MagicMock(return_value="project-1"),
    )
    existing_event = _event()

    assert service.resolve(existing_event) == "existing-session"
    assert "_session_just_materialized" not in existing_event.metadata

    session_manager.lookup_session_id.return_value = None
    session_manager.recover_session.return_value = None
    session_manager.register_session.return_value = "created-session"
    created_event = _event()

    assert service.resolve(created_event) == "created-session"
    assert created_event.metadata["_session_just_materialized"] is True


def _uncached_service() -> tuple[MagicMock, MagicMock, SessionLookupService]:
    session_manager = MagicMock()
    session_manager.get_session_id.return_value = None
    session_manager.lookup_session_id.return_value = None
    session_manager.recover_session.return_value = None
    session_manager.backfill_terminal_context.return_value = None
    session_manager.get.return_value.machine_id = _REAL_MACHINE_ID
    session_task_manager = MagicMock()
    session_task_manager.get_worked_on_task.return_value = None
    service = _service(
        session_manager,
        session_task_manager,
        MagicMock(return_value="project-1"),
    )
    return session_manager, session_task_manager, service


def _pane_event(
    event_type: HookEventType,
    *,
    session_id: str = "01a04561-child",
    source: SessionSource = SessionSource.GROK,
    machine_id: str = _REAL_MACHINE_ID,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=session_id,
        source=source,
        timestamp=datetime.now(UTC),
        machine_id=machine_id,
        data={
            "terminal_context": {
                "gobby_terminal_id": "d0c9bf38-1ca2-5197-9c87-14a5d0aff60d",
                "parent_pid": 64881,
            }
        },
        metadata={},
    )


def test_subagent_start_without_parent_does_not_materialize() -> None:
    session_manager, _, service = _uncached_service()
    event = _pane_event(HookEventType.SUBAGENT_START)

    result = service.resolve(event)

    assert result is None
    assert "_platform_session_id" not in event.metadata
    assert "_session_just_materialized" not in event.metadata
    session_manager.register_session.assert_not_called()


def test_tool_hook_without_parent_subagent_still_auto_registers() -> None:
    session_manager, _, service = _uncached_service()
    session_manager.db.fetchone.return_value = None
    session_manager.register_session.return_value = "created-session"
    event = _pane_event(HookEventType.BEFORE_TOOL, source=SessionSource.CLAUDE)

    result = service.resolve(event)

    assert result == "created-session"
    assert event.metadata["_session_just_materialized"] is True
    session_manager.register_session.assert_called_once()


@pytest.mark.parametrize(
    "event_type",
    [
        HookEventType.SESSION_START,
        HookEventType.BEFORE_TOOL,
        HookEventType.STOP,
        HookEventType.SESSION_END,
    ],
)
def test_hintless_grok_child_keeps_the_live_native_parent(event_type: HookEventType) -> None:
    from gobby.hooks.event_handlers import EventHandlers

    session_manager, session_tasks, service = _uncached_service()
    event = _pane_event(event_type, session_id="grok-child")
    context = event.data["terminal_context"]
    context["parent_create_time"] = 1789400000.0
    parent = SimpleNamespace(
        id="parent-session",
        external_id="grok-parent",
        machine_id=_REAL_MACHINE_ID,
        project_id="project-1",
        status="active",
        source="grok",
        session_type="terminal",
        terminal_context=dict(context),
    )
    session_manager.get.return_value = parent
    terminal = SimpleNamespace(
        session_id=parent.id,
        machine_id=_REAL_MACHINE_ID,
        backend="native",
        state="live",
    )
    with patch("gobby.storage.terminals.TerminalManager") as terminals:
        terminals.return_value.get.return_value = terminal
        assert service.resolve(event) == parent.id

    assert event.metadata["_native_subagent_binding"] is True
    if event_type == HookEventType.SESSION_START:
        handlers = EventHandlers(
            session_manager=session_manager, session_task_manager=session_tasks
        )
        assert handlers.handle_session_start(event).decision == "allow"
    session_manager.register_session.assert_not_called()
    session_manager.update_status_if_non_terminal.assert_not_called()


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("context", "parent_pid", 999999),
        ("context", "parent_create_time", 1789400001.0),
        ("owner", "machine_id", "another-machine"),
        ("owner", "source", "codex"),
        ("terminal", "machine_id", "another-machine"),
        ("terminal", "state", "exited"),
    ],
)
def test_hintless_grok_child_refuses_unproven_native_parent(
    target: str, field: str, value: object
) -> None:
    session_manager, _, service = _uncached_service()
    event = _pane_event(HookEventType.BEFORE_TOOL, session_id="grok-child")
    context = event.data["terminal_context"]
    context["parent_create_time"] = 1789400000.0
    owner_context = dict(context)
    owner = SimpleNamespace(
        id="parent-session",
        external_id="grok-parent",
        machine_id=_REAL_MACHINE_ID,
        source="grok",
        status="active",
        session_type="terminal",
        terminal_context=owner_context,
    )
    terminal = SimpleNamespace(session_id=owner.id, machine_id=_REAL_MACHINE_ID, state="live")
    if target == "context":
        owner_context[field] = value
    else:
        setattr(owner if target == "owner" else terminal, field, value)
    session_manager.get.return_value = owner

    with patch("gobby.storage.terminals.TerminalManager") as terminals:
        terminals.return_value.get.return_value = terminal
        assert service._bind_process_bound_grok_child(event, machine_id=_REAL_MACHINE_ID) is None

    assert "_native_subagent_binding" not in event.metadata
    session_manager.register_session.assert_not_called()


def test_grok_tool_hook_from_another_process_in_the_pane_auto_registers() -> None:
    """A new Grok process in a pane with a stale live owner is not its subagent."""
    session_manager, _, service = _uncached_service()
    parent = SimpleNamespace(
        id="parent-stale",
        external_id="01a0b000-stale-owner",
        machine_id=_REAL_MACHINE_ID,
        project_id="project-1",
        status="active",
        agent_run_id=None,
        agent_depth=0,
        terminal_context={
            "gobby_terminal_id": "d0c9bf38-1ca2-5197-9c87-14a5d0aff60d",
            "parent_pid": 51234,
            "parent_create_time": 1789400000.0,
        },
    )
    session_manager.get.return_value = parent
    session_manager.db.fetchone.return_value = {
        "stored": True,
        "variables": {"subagent_count": 3, "is_subagent": True},
    }
    session_manager.register_session.return_value = "created-session"
    event = _pane_event(HookEventType.BEFORE_TOOL, session_id="01a0b000-new-process")
    event.data["terminal_context"]["parent_pid"] = os.getpid()
    event.metadata["_platform_session_id"] = parent.id

    result = service.resolve(event)

    assert result == "created-session"
    assert "_native_subagent_binding" not in event.metadata
    session_manager.register_session.assert_called_once()


def test_grok_debug_trace_records_dispatched_subagent_start() -> None:
    payload = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "provider_contracts"
            / "grok"
            / "subagent-start-debug-trace.json"
        ).read_text()
    )
    observations = payload["observations"]
    lines = "\n".join(payload["sanitized_lines"])

    assert observations["subagent_start_dispatched"] is True
    assert observations["subagent_stop_dispatched"] is True
    assert observations["acp_blocking_events_omit_subagent_start"] is True
    assert "subagent_start" not in observations["acp_blocking_events"]
    assert payload["parent_session_id"] != payload["child_session_id"]
    assert "spawn_subagent" in lines
    assert "subagent_start" in lines
    assert "subagent_stop" in lines


def test_materialized_row_uses_normalized_deferred_identity() -> None:
    session_manager = MagicMock()
    session_manager.get_session_id.return_value = None
    session_manager.lookup_session_id.return_value = None
    session_manager.recover_session.return_value = None
    session_manager.register_session.return_value = "created-session"
    session_manager.backfill_terminal_context.return_value = None
    session_manager.get.return_value.machine_id = _REAL_MACHINE_ID
    session_task_manager = MagicMock()
    session_task_manager.get_worked_on_task.return_value = None
    service = _service(
        session_manager,
        session_task_manager,
        MagicMock(return_value="project-1"),
    )
    event = _event()
    event.event_type = HookEventType.BEFORE_AGENT
    event.cwd = "/work/repos/gobby"
    event.data = {
        "transcript_path": "/tmp/transcript.jsonl",
        "terminal_context": {"gobby_terminal_id": "1e557198-09a7-56ec-ba24-bb707c6faf68"},
    }

    assert service.resolve(event) == "created-session"
    session_manager.register_session.assert_called_once_with(
        external_id="claude-external",
        machine_id="21000000-0000-4000-8000-000000000009",
        project_id="project-1",
        transcript_path="/tmp/transcript.jsonl",
        source="claude",
        project_path="/work/repos/gobby",
        # The pane id waits for the activation bind to prove it.
        terminal_context={"cwd": "/work/repos/gobby"},
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
    session_manager.get.return_value = canonical
    session_manager.backfill_terminal_context.return_value = canonical
    session_task_manager = MagicMock()
    session_task_manager.get_worked_on_task.return_value = None
    service = _service(
        session_manager,
        session_task_manager,
        MagicMock(return_value="project-1"),
    )
    event = _event()
    event.data["terminal_context"] = {
        "gobby_terminal_id": "1e557198-09a7-56ec-ba24-bb707c6faf68",
        "parent_pid": 1234,
        "parent_create_time": 5678.0,
    }

    result = service.resolve(event)

    assert result == canonical.id
    assert event.session_id == canonical.external_id
    assert event.metadata["_observed_external_id"] == "claude-external"
    assert event.metadata["_platform_session_id"] == canonical.id
    session_manager.register_session.assert_not_called()


def test_task_context_preserves_explicit_task_and_enriches_matching_link() -> None:
    session_manager = MagicMock()
    session_manager.get.return_value = SimpleNamespace(
        id="platform-session", project_id="project-1", machine_id=_REAL_MACHINE_ID
    )
    session_manager.backfill_terminal_context.return_value = None
    explicit_task = SimpleNamespace(
        id="task-explicit",
        title="Explicit event task",
        closed_at=None,
        is_escalated=False,
        active_blocked_by=[],
    )
    session_task_manager = MagicMock()
    # The matching link is chosen in SQL (tests/storage/test_storage_session_tasks.py).
    session_task_manager.get_worked_on_task.return_value = explicit_task
    service = _service(session_manager, session_task_manager, MagicMock(return_value="project-1"))
    event = _event({"_platform_session_id": "platform-session"})
    event.task_id = explicit_task.id

    service.resolve(event)

    session_task_manager.get_worked_on_task.assert_called_once_with(
        "platform-session", "task-explicit"
    )
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
    session_manager.get.return_value = recovered
    session_manager.backfill_terminal_context.return_value = recovered
    session_task_manager = MagicMock()
    session_task_manager.get_worked_on_task.return_value = None
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
        machine_id=_REAL_MACHINE_ID,
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
        machine_id=_REAL_MACHINE_ID,
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
        machine_id=_REAL_MACHINE_ID,
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


def test_first_seen_thread_registers_without_its_unbound_pane_id(
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_db: HubDatabase,
) -> None:
    """A thread first seen by a tool hook stores no pane id its row is not bound to.

    Registration runs before any bind proof, so a stored pane id would let the wake
    path reach a pane the bind may still refuse. The hook keeps its own context, so
    the same hook's activation still binds from it.
    """
    with patch("gobby.utils.machine_id._cached_machine_id", _REAL_MACHINE_ID):
        project_id = isolated_checkout_factory(temp_db, "first-seen-project").project.id
        storage_sessions = SessionManager(temp_db)
        terminals = TerminalManager(temp_db)
        pane_id = str(uuid4())
        pane = terminals.create_pending(
            terminal_id=pane_id,
            project_id=project_id,
            backend="native",
            ownership="gobby",
            spawn_key=pane_id,
        )
        assert pane.attempt_started_at is not None
        assert (
            terminals.record_process(
                pane_id,
                {"pgid": 101, "host_terminal_id": pane_id},
                attempt_generation=pane.attempt_generation,
                attempt_started_at=pane.attempt_started_at,
            )
            is not None
        )
        session_manager = cast(HookSessionManager, storage_sessions)
        service = SessionLookupService(
            session_manager=session_manager,
            session_coordinator=SessionCoordinator(session_storage=session_manager),
            session_task_manager=SessionTaskManager(temp_db),
            resolve_project_id=lambda *_: project_id,
            logger=logging.getLogger("tests.session_lookup.first_seen"),
        )
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="first-seen-claude-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"terminal_context": {"gobby_terminal_id": pane_id}},
            machine_id=_REAL_MACHINE_ID,
            metadata={},
        )

        resolved = service.resolve(event)

        assert isinstance(resolved, str) and resolved
        assert event.metadata["_session_just_materialized"] is True
        stored = storage_sessions.get(resolved)
        assert stored is not None
        assert "gobby_terminal_id" not in (stored.terminal_context or {})
        assert terminals.resolve_live_for_session(stored) is None
        assert event.data["terminal_context"]["gobby_terminal_id"] == pane_id


def test_expired_session_recovery_reports_status_through_the_real_path(
    isolated_checkout_factory: IsolatedCheckoutFactory,
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
        project_id = isolated_checkout_factory(temp_db, "lookup-project").project.id
        storage_sessions = SessionManager(temp_db)
        # hook_manager.py casts at this same boundary: SessionManager serves the
        # HookSessionManager protocol at runtime without nominally declaring it.
        session_manager = cast(HookSessionManager, storage_sessions)
        terminal_context = {"gobby_terminal_id": "d9cfb6eb-d271-57ab-8bc2-60cba3a53552"}
        registered = storage_sessions.register(
            external_id="retired-claude-session",
            machine_id=_REAL_MACHINE_ID,
            source="claude",
            project_id=project_id,
            terminal_context=terminal_context,
        )
        assert storage_sessions.mark_session_expired(registered.id, cause="context_reuse")

        logger = logging.getLogger("tests.session_lookup.integration")
        service = SessionLookupService(
            session_manager=session_manager,
            session_coordinator=SessionCoordinator(session_storage=session_manager),
            session_task_manager=SessionTaskManager(temp_db),
            resolve_project_id=lambda *_: project_id,
            logger=logger,
        )
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="retired-claude-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"terminal_context": dict(terminal_context)},
            machine_id=_REAL_MACHINE_ID,
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

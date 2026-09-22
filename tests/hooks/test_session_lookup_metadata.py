"""Tests for hook session lookup metadata preservation."""

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import psutil
import pytest

from gobby.hooks.event_handlers import EventHandlers
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
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit

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
        data={"terminal_context": {"tmux_pane": "%1"}},
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
    session_manager.backfill_terminal_context.return_value = (None, False)
    session_task_manager = MagicMock()
    session_task_manager.get_session_tasks.return_value = []
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
    session_manager.find_live_interactive_pane_owner.return_value = None
    session_manager.backfill_terminal_context.return_value = (None, False)
    session_task_manager = MagicMock()
    session_task_manager.get_session_tasks.return_value = []
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
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=session_id,
        source=source,
        timestamp=datetime.now(UTC),
        machine_id=_REAL_MACHINE_ID,
        data={
            "terminal_context": {
                "tmux_pane": "%90",
                "tmux_socket_path": "/tmp/tmux-501/default",
                "parent_pid": 64881,
            }
        },
        metadata={},
    )


def _live_grok_terminal_context() -> dict[str, Any]:
    return {
        "tmux_pane": "%90",
        "tmux_socket_path": "/tmp/tmux-501/default",
        "parent_pid": os.getpid(),
        "parent_create_time": psutil.Process(os.getpid()).create_time(),
    }


def test_subagent_start_with_parent_tty_binds_without_registering() -> None:
    session_manager, _, service = _uncached_service()
    parent = SimpleNamespace(id="parent-live", status="active", agent_run_id=None, agent_depth=0)
    session_manager.find_live_interactive_pane_owner.return_value = parent
    event = _pane_event(HookEventType.SUBAGENT_START)

    result = service.resolve(event)

    assert result == "parent-live"
    assert event.metadata["_platform_session_id"] == "parent-live"
    assert event.metadata["_native_subagent_binding"] is True
    assert "_session_just_materialized" not in event.metadata
    session_manager.register_session.assert_not_called()


def test_subagent_start_without_parent_does_not_materialize() -> None:
    session_manager, _, service = _uncached_service()
    event = _pane_event(HookEventType.SUBAGENT_START)

    result = service.resolve(event)

    assert result is None
    assert "_platform_session_id" not in event.metadata
    assert "_session_just_materialized" not in event.metadata
    session_manager.register_session.assert_not_called()


def test_tool_hook_with_active_parent_subagent_binds_to_parent() -> None:
    session_manager, _, service = _uncached_service()
    parent = SimpleNamespace(id="parent-live", status="active", agent_run_id=None, agent_depth=0)
    session_manager.find_live_interactive_pane_owner.return_value = parent
    session_manager.db.fetchone.return_value = {
        "variables": {"subagent_count": 1, "is_subagent": True}
    }
    event = _pane_event(HookEventType.BEFORE_TOOL, source=SessionSource.CLAUDE)

    result = service.resolve(event)

    assert result == "parent-live"
    session_manager.register_session.assert_not_called()


def test_tool_hook_without_parent_subagent_still_auto_registers() -> None:
    session_manager, _, service = _uncached_service()
    parent = SimpleNamespace(id="parent-live", status="active", agent_run_id=None, agent_depth=0)
    session_manager.find_live_interactive_pane_owner.return_value = parent
    session_manager.db.fetchone.return_value = None
    session_manager.register_session.return_value = "created-session"
    event = _pane_event(HookEventType.BEFORE_TOOL, source=SessionSource.CLAUDE)

    result = service.resolve(event)

    assert result == "created-session"
    assert event.metadata["_session_just_materialized"] is True
    session_manager.register_session.assert_called_once()


def test_grok_tool_hook_with_parent_tty_binds_without_prior_subagent_start() -> None:
    payload = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "provider_contracts"
            / "grok"
            / "subagent-start-drop-summary.json"
        ).read_text()
    )["sanitized_child_payload"]
    # Enrichment verifies parent_create_time against the live process table, so
    # the child hook must name a process that exists: this test's own.
    payload["terminal_context"]["parent_pid"] = os.getpid()
    session_manager, _, service = _uncached_service()
    parent = SimpleNamespace(
        id="parent-live",
        status="active",
        agent_run_id=None,
        agent_depth=0,
        # The child conversation runs inside the parent's Grok process.
        terminal_context={
            **payload["terminal_context"],
            "parent_create_time": psutil.Process(os.getpid()).create_time(),
        },
    )
    session_manager.find_live_interactive_pane_owner.return_value = parent
    session_manager.db.fetchone.return_value = None
    event = _pane_event(HookEventType.BEFORE_TOOL, session_id=payload["sessionId"])
    event.data["terminal_context"] = payload["terminal_context"]

    result = service.resolve(event)

    assert result == "parent-live"
    assert event.metadata["_native_subagent_binding"] is True
    assert "_session_just_materialized" not in event.metadata
    session_manager.register_session.assert_not_called()


def test_grok_tool_hook_from_another_process_in_the_pane_auto_registers() -> None:
    """A new Grok process in a pane with a stale live owner is not its subagent."""
    session_manager, _, service = _uncached_service()
    parent = SimpleNamespace(
        id="parent-stale",
        external_id="01a0b000-stale-owner",
        project_id="project-1",
        status="active",
        agent_run_id=None,
        agent_depth=0,
        terminal_context={
            "tmux_pane": "%90",
            "tmux_socket_path": "/tmp/tmux-501/default",
            "parent_pid": 51234,
            "parent_create_time": 1789400000.0,
        },
    )
    session_manager.get.return_value = parent
    session_manager.find_live_interactive_pane_owner.return_value = parent
    session_manager.db.fetchone.return_value = {
        "variables": {"subagent_count": 3, "is_subagent": True}
    }
    session_manager.register_session.return_value = "created-session"
    event = _pane_event(HookEventType.BEFORE_TOOL, session_id="01a0b000-new-process")
    event.data["terminal_context"]["parent_pid"] = os.getpid()
    event.metadata["_platform_session_id"] = parent.id

    result = service.resolve(event)

    assert result == "created-session"
    assert "_native_subagent_binding" not in event.metadata
    session_manager.register_session.assert_called_once()


def test_spawned_grok_parent_survives_first_of_three_process_bound_children() -> None:
    session_manager, session_task_manager, service = _uncached_service()
    parent_context = _live_grok_terminal_context()
    parent = SimpleNamespace(
        id="1040f8b8-parent-session",
        external_id="01a0c9b7-8671-7d60-a15c-e25ed5209883",
        project_id="project-1",
        status="active",
        session_type="terminal",
        agent_run_id="b8fba33c-1661-4a7e-b835-6e03e0dcd59c",
        created_at=datetime.now(UTC),
        terminal_context=parent_context,
    )
    session_manager.get.return_value = parent
    session_manager.find_live_interactive_pane_owner.return_value = parent
    session_task_manager.get_session_tasks.return_value = []

    coordinator = MagicMock()
    task_manager = MagicMock()
    task_manager.list_tasks.return_value = []
    terminal_manager = MagicMock()
    terminal_manager.get_live_for_session.return_value = SimpleNamespace(
        id="2228eb40-parent-terminal",
        ownership="gobby",
        agent_run_id=parent.agent_run_id,
    )
    handlers = EventHandlers(
        session_manager=session_manager,
        session_task_manager=session_task_manager,
        session_coordinator=coordinator,
        task_manager=task_manager,
        terminal_manager=terminal_manager,
    )

    child_ids = (
        "01a0c9bd-8c02-7242-94ce-fddc90374322",
        "01a0c9bd-8c02-7242-94ce-fdef9e232d6a",
        "01a0c9bd-8c02-7242-94ce-fdf9b83b402a",
    )
    for child_id in child_ids:
        child_event = _pane_event(HookEventType.BEFORE_TOOL, session_id=child_id)
        child_event.data["terminal_context"] = dict(parent_context)
        child_event.metadata["_platform_session_id"] = parent.id

        assert service.resolve(child_event) == parent.id
        assert child_event.metadata["_native_subagent_binding"] is True

    child_stop = _pane_event(HookEventType.STOP, session_id=child_ids[0])
    child_stop.data["terminal_context"] = dict(parent_context)
    child_stop.metadata["_platform_session_id"] = parent.id
    assert service.resolve(child_stop) == parent.id
    with (
        patch.object(handlers, "_end_turn_lifecycle") as end_turn,
        patch("gobby.hooks.event_handlers._agent.retire_session_hook_effects") as retire,
    ):
        assert handlers.handle_stop(child_stop).decision == "allow"
    end_turn.assert_not_called()
    retire.assert_not_called()

    child_end = _pane_event(HookEventType.SESSION_END, session_id=child_ids[0])
    child_end.data["terminal_context"] = dict(parent_context)
    child_end.metadata["_platform_session_id"] = parent.id
    assert service.resolve(child_end) == parent.id
    assert handlers.handle_session_end(child_end).decision == "allow"

    coordinator.complete_agent_run.assert_not_called()
    session_manager.update_status_if_non_terminal.assert_not_called()
    terminal_manager.mark_exited.assert_not_called()

    parent_end = _pane_event(HookEventType.SESSION_END, session_id=parent.external_id)
    parent_end.data["terminal_context"] = dict(parent_context)
    parent_end.metadata["_platform_session_id"] = parent.id
    assert service.resolve(parent_end) == parent.id
    assert "_native_subagent_binding" not in parent_end.metadata
    assert handlers.handle_session_end(parent_end).decision == "allow"

    coordinator.complete_agent_run.assert_called_once_with(parent)
    session_manager.update_status_if_non_terminal.assert_called_once_with(parent.id, "expired")
    terminal_manager.mark_exited.assert_called_once_with("2228eb40-parent-terminal")


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
    session_manager.backfill_terminal_context.return_value = (None, False)
    session_task_manager = MagicMock()
    session_task_manager.get_session_tasks.return_value = []
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
        "terminal_context": {"tmux_pane": "%1"},
    }

    assert service.resolve(event) == "created-session"
    session_manager.register_session.assert_called_once_with(
        external_id="claude-external",
        machine_id="21000000-0000-4000-8000-000000000009",
        project_id="project-1",
        transcript_path="/tmp/transcript.jsonl",
        source="claude",
        project_path="/work/repos/gobby",
        terminal_context={"tmux_pane": "%1", "cwd": "/work/repos/gobby"},
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
        terminal_context = {"tmux_pane": "%11", "tmux_socket_path": "/tmp/tmux-501/gobby"}
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

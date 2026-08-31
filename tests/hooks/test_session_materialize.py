"""Deferred first-activity materialization."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.event_handlers._session_start.handoff import SessionStartResolution
from gobby.hooks.event_handlers._session_start.materialize import activate_materialized_session
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.session_materialize import activate_deferred_session
from gobby.sessions.clear_continuation import stage_clear_attempt
from gobby.sessions.handoff import consume_pending_handoff, render_handoff_markdown
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.unit

_DERIVED = "/home/user/.grok/sessions/%2Frepo/grok-external/updates.jsonl"


def _manager(session: SimpleNamespace, updated: SimpleNamespace | None) -> MagicMock:
    manager = MagicMock()
    manager._session_manager.get.return_value = session
    manager._session_manager.update.return_value = updated
    manager._event_handlers._derive_transcript_path.return_value = _DERIVED
    manager._event_handlers._activate_materialized_session.return_value = []
    manager._event_handlers._compose_session_response.return_value = HookResponse(
        decision="allow",
        system_message="Gobby Session ID: #7",
    )
    manager._evaluate_workflow_rules.return_value = (None, None)
    manager._evaluate_blocking_webhooks.return_value = None
    manager.get_machine_id.return_value = "machine-1"
    return manager


def _event(data: dict[str, object], *, source: SessionSource = SessionSource.GROK) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="grok-external",
        source=source,
        timestamp=datetime.now(UTC),
        data=data,
        machine_id="machine-1",
        project_id="project-1",
        metadata={"_platform_session_id": "platform-session"},
    )


def test_deferred_grok_session_derives_and_persists_transcript_path() -> None:
    session = SimpleNamespace(
        id="platform-session",
        project_id="project-1",
        parent_session_id=None,
        transcript_path=None,
    )
    updated = SimpleNamespace(**{**vars(session), "transcript_path": _DERIVED})
    manager = _manager(session, updated)
    event = _event({"prompt": "hello", "cwd": "/repo"})

    assert activate_deferred_session(manager, event, BlockingEffectDeadline(123.0)) is None

    manager._event_handlers._derive_transcript_path.assert_called_once_with(
        "grok",
        event.data,
        "grok-external",
        owner_machine_id="machine-1",
        local_machine_id="machine-1",
    )
    manager._session_manager.update.assert_called_once_with(
        session_id="platform-session",
        transcript_path=_DERIVED,
    )
    activate = manager._event_handlers._activate_materialized_session.call_args.kwargs
    assert activate["transcript_path"] == _DERIVED
    assert activate["session_obj"] is updated


def test_native_transcript_path_skips_derivation() -> None:
    session = SimpleNamespace(
        id="platform-session",
        project_id="project-1",
        parent_session_id=None,
        transcript_path=None,
    )
    manager = _manager(session, None)
    event = _event({"prompt": "hello", "cwd": "/repo", "transcript_path": "/repo/t.jsonl"})

    assert activate_deferred_session(manager, event, BlockingEffectDeadline(123.0)) is None

    manager._event_handlers._derive_transcript_path.assert_not_called()
    manager._session_manager.update.assert_not_called()
    activate = manager._event_handlers._activate_materialized_session.call_args.kwargs
    assert activate["transcript_path"] == "/repo/t.jsonl"
    assert activate["resolution"] is None


def test_deferred_activation_passes_matching_clear_resolution() -> None:
    session = SimpleNamespace(
        id="platform-session",
        project_id="project-1",
        parent_session_id=None,
        transcript_path="/repo/t.jsonl",
    )
    manager = _manager(session, None)
    predecessor = SimpleNamespace(id="predecessor-sess")
    resolution = SessionStartResolution(
        session=None,
        session_source="clear",
        clear_predecessor=predecessor,
        clear_attempt_id="attempt-1",
    )
    event = _event(
        {
            "prompt": "hello",
            "cwd": "/repo",
            "transcript_path": "/repo/t.jsonl",
            "terminal_context": {"tmux_pane": "%100"},
        }
    )

    with patch(
        "gobby.hooks.session_materialize.resolve_matching_clear_continuation",
        return_value=resolution,
    ) as mock_resolve:
        assert activate_deferred_session(manager, event, BlockingEffectDeadline(123.0)) is None

    mock_resolve.assert_called_once()
    activate = manager._event_handlers._activate_materialized_session.call_args.kwargs
    assert activate["resolution"] is resolution


@patch(
    "gobby.hooks.event_handlers._session_start.materialize.classify_session_start_context",
    return_value=SimpleNamespace(mode="full"),
)
@patch(
    "gobby.hooks.event_handlers._session_start.materialize.expire_stale_terminal_sessions_for_context"
)
@patch("gobby.hooks.event_handlers._session_start.materialize.schedule_handoff_continuation")
def test_startup_source_with_clear_resolution_binds_without_prompt(
    mock_schedule: MagicMock,
    _mock_expire: MagicMock,
    _mock_classify: MagicMock,
    temp_db: HubDatabase,
) -> None:
    """First-activity materialization binds parentage without typing a second prompt."""
    machine_id = require_machine_id()
    project = LocalProjectManager(temp_db).create(name="clear-bind", repo_path="/tmp/clear-bind")
    sessions = SessionManager(temp_db)
    term = {
        "tmux_pane": "%100",
        "tmux_socket_path": "/tmp/tmux",
        "parent_pid": 10322,
        "parent_create_time": 1.0,
    }
    predecessor_id = sessions.register_session(
        external_id="pred-ext",
        machine_id=machine_id,
        source="grok",
        project_id=project.id,
        terminal_context=term,
    )
    stage_clear_attempt(
        temp_db,
        predecessor_id,
        attempt_id="attempt-1",
        handoff_markdown=render_handoff_markdown(
            current_state="Ready.",
            next_steps=["Continue."],
        ),
        observations=[],
        terminal_context=term,
        chat_context=None,
    )
    successor_id = sessions.register_session(
        external_id="succ-ext",
        machine_id=machine_id,
        source="grok",
        project_id=project.id,
        terminal_context=term,
    )
    predecessor = sessions.get(predecessor_id)
    successor = sessions.get(successor_id)
    assert predecessor is not None
    assert successor is not None

    handler = MagicMock()
    handler.terminal_manager = None
    handler._session_manager = sessions
    handler._session_coordinator = None
    handler._resolve_message_processor.return_value = None
    handler._build_claimed_task_context.return_value = None
    event = _event(
        {
            "source": "startup",
            "skip_default_agent_activation": True,
            "cwd": "/tmp/clear-bind",
        }
    )
    event.task_id = None
    resolution = SessionStartResolution(
        session=None,
        session_source="clear",
        clear_predecessor=predecessor,
        clear_attempt_id="attempt-1",
    )

    with (
        patch("gobby.hooks.event_handlers._session_start.materialize._seed_parent_turn_seq"),
        patch("gobby.hooks.event_handlers._session_start.materialize._seed_wiki_overview_var"),
        patch("gobby.hooks.event_handlers._session_start.materialize.seed_user_profile_content"),
        patch(
            "gobby.hooks.event_handlers._session_start.materialize.prepare_compact_continuation_variables"
        ),
        patch(
            "gobby.hooks.event_handlers._session_start.materialize._schedule_tmux_window_rename_for_session"
        ),
    ):
        activate_materialized_session(
            handler,
            event,
            successor_id,
            resolution=resolution,
            session_obj=successor,
            project_id=project.id,
            transcript_path=None,
        )

    rebound = sessions.get(successor_id)
    assert rebound is not None
    assert rebound.parent_session_id == predecessor_id
    consumed = consume_pending_handoff(temp_db, successor_id)
    assert consumed is not None
    assert consumed.session_id == predecessor_id
    mock_schedule.assert_not_called()


@pytest.mark.parametrize(
    ("cli", "session_start_types_prompt"),
    [(SessionSource.GROK, True), (SessionSource.CODEX, False)],
)
def test_clear_session_start_types_pull_prompt_only_when_none_is_in_flight(
    cli: SessionSource,
    session_start_types_prompt: bool,
    temp_db: HubDatabase,
) -> None:
    """Codex SessionStart fires on the successor's first submitted prompt, so one is already in flight."""
    machine_id = require_machine_id()
    project = LocalProjectManager(temp_db).create(
        name=f"clear-{cli.value}", repo_path=f"/tmp/clear-{cli.value}"
    )
    sessions = SessionManager(temp_db)
    term = {
        "tmux_pane": "%101",
        "tmux_socket_path": "/tmp/tmux",
        "parent_pid": 10323,
        "parent_create_time": 1.0,
    }
    predecessor_id = sessions.register_session(
        external_id="pred-ext",
        machine_id=machine_id,
        source=cli.value,
        project_id=project.id,
        terminal_context=term,
    )
    stage_clear_attempt(
        temp_db,
        predecessor_id,
        attempt_id="attempt-1",
        handoff_markdown=render_handoff_markdown(current_state="Ready.", next_steps=["Continue."]),
        observations=[],
        terminal_context=term,
        chat_context=None,
    )
    successor_id = sessions.register_session(
        external_id="succ-ext",
        machine_id=machine_id,
        source=cli.value,
        project_id=project.id,
        terminal_context=term,
    )
    predecessor = sessions.get(predecessor_id)
    successor = sessions.get(successor_id)
    assert predecessor is not None
    assert successor is not None

    handler = MagicMock()
    handler.terminal_manager = None
    handler._session_manager = sessions
    handler._session_coordinator = None
    handler._resolve_message_processor.return_value = None
    handler._build_claimed_task_context.return_value = None
    event = _event(
        {
            "source": "clear",
            "skip_default_agent_activation": True,
            "cwd": f"/tmp/clear-{cli.value}",
        },
        source=cli,
    )
    event.task_id = None
    resolution = SessionStartResolution(
        session=None,
        session_source="clear",
        clear_predecessor=predecessor,
        clear_attempt_id="attempt-1",
    )
    materialize = "gobby.hooks.event_handlers._session_start.materialize"

    with (
        patch(f"{materialize}._seed_parent_turn_seq"),
        patch(f"{materialize}._seed_wiki_overview_var"),
        patch(f"{materialize}.seed_user_profile_content"),
        patch(f"{materialize}.prepare_compact_continuation_variables"),
        patch(f"{materialize}._schedule_tmux_window_rename_for_session"),
        patch(f"{materialize}.classify_session_start_context"),
        patch(f"{materialize}.expire_stale_terminal_sessions_for_context"),
        patch(f"{materialize}.schedule_handoff_continuation") as mock_schedule,
    ):
        activate_materialized_session(
            handler,
            event,
            successor_id,
            resolution=resolution,
            session_obj=successor,
            project_id=project.id,
            transcript_path=None,
        )

    rebound = sessions.get(successor_id)
    assert rebound is not None
    assert rebound.parent_session_id == predecessor_id
    assert mock_schedule.called is session_start_types_prompt

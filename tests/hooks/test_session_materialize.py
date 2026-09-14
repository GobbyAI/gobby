"""Deferred first-activity materialization."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.event_handlers._session_start.context import classify_session_start_context
from gobby.hooks.event_handlers._session_start.handoff import SessionStartResolution
from gobby.hooks.event_handlers._session_start.materialize import activate_materialized_session
from gobby.hooks.event_handlers._session_start.terminal_runtime import (
    expire_stale_terminal_sessions_for_context,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.session_materialize import activate_deferred_session, has_deferred_help_activation
from gobby.sessions.clear_continuation import (
    CLEAR_ATTEMPT_VARIABLE,
    resolve_clear_continuation,
    stage_clear_attempt,
    take_clear_handoff_marker,
)
from gobby.sessions.handoff import (
    HANDOFF_PULL_PENDING_VARIABLE,
    consume_pending_handoff,
)
from gobby.sessions.handoff_records import build_handoff_payload
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import (
    IsolatedCheckoutFactory,
    install_isolated_checkout_project,
)
from tests.hooks.test_session_start_handlers import _register_context_claim_session

pytestmark = pytest.mark.unit

_DERIVED = "/home/user/.grok/sessions/%2Frepo/grok-external/updates.jsonl"
_ATTEMPT_ID = "1" * 32


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


@pytest.mark.parametrize("suffix", ["", " help"])
@pytest.mark.parametrize("source", list(SessionSource))
def test_help_defers_startup_until_work(source: SessionSource, suffix: str) -> None:
    session = SimpleNamespace(
        id="platform-session", project_id=None, parent_session_id=None, transcript_path=_DERIVED
    )
    manager = _manager(session, None)
    manager._event_handlers._compose_session_response.return_value = HookResponse(
        context="Pending startup instructions", system_message="Session identity"
    )
    prompt = ("$gobby" if source == SessionSource.CODEX else "/gobby") + suffix
    event = _event({"prompt": prompt}, source=source)
    state: dict[str, object] = {}

    def merge(session_id: str, values: dict[str, object]) -> None:
        state.update(values)

    with patch("gobby.hooks.session_materialize.SessionVariableManager") as variables:
        variables.return_value.get_variables.return_value = state
        variables.return_value.merge_variables.side_effect = merge
        assert activate_deferred_session(manager, event, BlockingEffectDeadline(123.0)) is None
        assert not has_deferred_help_activation(manager, event)
        assert "_startup_context" not in event.metadata
        manager._event_handlers._activate_materialized_session.assert_not_called()
        manager._evaluate_workflow_rules.assert_not_called()

        event.data["prompt"] = "Implement the change"
        assert has_deferred_help_activation(manager, event)
        assert activate_deferred_session(manager, event, BlockingEffectDeadline(123.0)) is None
        assert event.metadata["_startup_context"] == "Pending startup instructions"
        assert event.metadata["_startup_system_message"] == "Session identity"
        assert not has_deferred_help_activation(manager, event)
        manager._event_handlers._activate_materialized_session.assert_called_once()


def test_help_transcript_does_not_consume_startup_claim(
    temp_db: HubDatabase, isolated_checkout_factory: IsolatedCheckoutFactory
) -> None:
    session_id = _register_context_claim_session(
        isolated_checkout_factory, temp_db, external_id="help-before-startup"
    )
    sessions = SessionManager(temp_db)
    session = SimpleNamespace(startup_claim_state="idle", message_count=4, turn_count=2)
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(session_id, {"_help_deferred_activation": True})
    handler = SimpleNamespace(_session_manager=sessions, logger=MagicMock())
    decision = classify_session_start_context(
        handler,
        session_id=session_id,
        session=session,
        session_source="startup",
        is_existing_session=True,
    )
    assert decision.mode == "full"
    assert decision.claim is not None
    assert decision.claim.state == "claimed"


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
        stored_path=None,
    )
    manager._session_manager.update.assert_called_once_with(
        session_id="platform-session",
        transcript_path=_DERIVED,
    )
    activate = manager._event_handlers._activate_materialized_session.call_args.kwargs
    assert activate["transcript_path"] == _DERIVED
    assert activate["session_obj"] is updated


def test_native_transcript_path_is_classified_before_use() -> None:
    native = "/repo/t.jsonl"
    session = SimpleNamespace(
        id="platform-session",
        project_id="project-1",
        parent_session_id=None,
        transcript_path=None,
    )
    manager = _manager(session, None)
    manager._event_handlers._derive_transcript_path.return_value = native
    event = _event({"prompt": "hello", "cwd": "/repo", "transcript_path": native})

    assert activate_deferred_session(manager, event, BlockingEffectDeadline(123.0)) is None

    manager._event_handlers._derive_transcript_path.assert_called_once_with(
        "grok",
        event.data,
        "grok-external",
        owner_machine_id="machine-1",
        local_machine_id="machine-1",
        stored_path=None,
    )
    manager._session_manager.update.assert_called_once_with(
        session_id="platform-session",
        transcript_path=native,
    )
    activate = manager._event_handlers._activate_materialized_session.call_args.kwargs
    assert activate["transcript_path"] == native
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
        clear_attempt_id=_ATTEMPT_ID,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-activity materialization binds parentage without typing a second prompt."""
    checkout = install_isolated_checkout_project(
        temp_db, tmp_path / "clear-bind", name="clear-bind", monkeypatch=monkeypatch
    )
    machine_id = checkout.machine_id
    project = checkout.project
    root = checkout.root_path
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
        attempt_id=_ATTEMPT_ID,
        handoff=build_handoff_payload(
            current_state="Ready.",
            next_steps=["Continue."],
        ),
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
            "cwd": root,
        }
    )
    event.task_id = None
    resolution = SessionStartResolution(
        session=None,
        session_source="clear",
        clear_predecessor=predecessor,
        clear_attempt_id=_ATTEMPT_ID,
    )

    with (
        patch("gobby.hooks.event_handlers._session_start.materialize._seed_parent_turn_seq"),
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex SessionStart fires on the successor's first submitted prompt, so one is already in flight."""
    checkout = install_isolated_checkout_project(
        temp_db, tmp_path / f"clear-{cli.value}", name=f"clear-{cli.value}", monkeypatch=monkeypatch
    )
    machine_id = checkout.machine_id
    project = checkout.project
    root = checkout.root_path
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
        attempt_id=_ATTEMPT_ID,
        handoff=build_handoff_payload(current_state="Ready.", next_steps=["Continue."]),
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
            "cwd": root,
        },
        source=cli,
    )
    event.task_id = None
    resolution = SessionStartResolution(
        session=None,
        session_source="clear",
        clear_predecessor=predecessor,
        clear_attempt_id=_ATTEMPT_ID,
    )
    materialize = "gobby.hooks.event_handlers._session_start.materialize"

    with (
        patch(f"{materialize}._seed_parent_turn_seq"),
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


_MATERIALIZE = "gobby.hooks.event_handlers._session_start.materialize"
# Activation side effects every clear-bind test below stubs unless a test names
# one in ``overrides`` (``None`` keeps the real function).
_ACTIVATION_STUBS = (
    "_seed_parent_turn_seq",
    "seed_user_profile_content",
    "prepare_compact_continuation_variables",
    "_schedule_tmux_window_rename_for_session",
    "classify_session_start_context",
    "schedule_handoff_continuation",
    "expire_stale_terminal_sessions_for_context",
)


@dataclass
class _StagedClear:
    """A clear predecessor staged on one pane plus the means to add same-pane rows."""

    sessions: SessionManager
    machine_id: str
    project_id: str
    root: str
    term: dict[str, object]
    predecessor_id: str = ""

    def register(self, external_id: str) -> str:
        return self.sessions.register_session(
            external_id=external_id,
            machine_id=self.machine_id,
            source="grok",
            project_id=self.project_id,
            terminal_context=self.term,
        )

    def status(self, session_id: str) -> str | None:
        session = self.sessions.get(session_id)
        return None if session is None else session.status

    def resolution(self) -> SessionStartResolution:
        return SessionStartResolution(
            session=None,
            session_source="clear",
            clear_predecessor=self.sessions.get(self.predecessor_id),
            clear_attempt_id=_ATTEMPT_ID,
        )


def _staged_clear(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    pane: str,
) -> _StagedClear:
    checkout = install_isolated_checkout_project(
        temp_db, tmp_path / name, name=name, monkeypatch=monkeypatch
    )
    staged = _StagedClear(
        sessions=SessionManager(temp_db),
        machine_id=checkout.machine_id,
        project_id=checkout.project.id,
        root=checkout.root_path,
        term={
            "tmux_pane": pane,
            "tmux_socket_path": "/tmp/tmux",
            "parent_pid": 10324,
            "parent_create_time": 1.0,
        },
    )
    staged.predecessor_id = staged.register("pred-ext")
    stage_clear_attempt(
        temp_db,
        staged.predecessor_id,
        attempt_id=_ATTEMPT_ID,
        handoff=build_handoff_payload(current_state="Ready.", next_steps=["Continue."]),
        terminal_context=staged.term,
        chat_context=None,
    )
    return staged


def _handler(sessions: SessionManager) -> MagicMock:
    handler = MagicMock()
    handler.terminal_manager = None
    handler._session_manager = sessions
    handler._session_coordinator = None
    handler._resolve_message_processor.return_value = None
    handler._build_claimed_task_context.return_value = None
    return handler


def _activate_clear_successor(
    staged: _StagedClear,
    handler: MagicMock,
    session_id: str,
    resolution: SessionStartResolution,
    *,
    overrides: dict[str, object | None] | None = None,
) -> None:
    overrides = overrides or {}
    session = staged.sessions.get(session_id)
    assert session is not None
    event = _event({"source": "clear", "skip_default_agent_activation": True, "cwd": staged.root})
    event.task_id = None
    with ExitStack() as stack:
        for name in dict.fromkeys((*_ACTIVATION_STUBS, *overrides)):
            replacement = overrides.get(name, MagicMock())
            if replacement is None:
                continue
            stack.enter_context(patch(f"{_MATERIALIZE}.{name}", replacement))
        activate_materialized_session(
            handler,
            event,
            session_id,
            resolution=resolution,
            session_obj=session,
            project_id=staged.project_id,
            transcript_path=None,
            terminal_context=staged.term,
        )


def test_context_reuse_expiry_runs_after_the_successor_binds(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When expiry scans the pane the newcomer is parented and the predecessor already expired."""
    staged = _staged_clear(temp_db, tmp_path, monkeypatch, name="expiry-order", pane="%102")
    successor_id = staged.register("succ-ext")
    assert staged.status(staged.predecessor_id) == "awaiting_handoff"
    seen: list[tuple[str | None, str | None]] = []

    def record(handler: object, **kwargs: object) -> None:
        successor = staged.sessions.get(successor_id)
        parent = None if successor is None else successor.parent_session_id
        seen.append((parent, staged.status(staged.predecessor_id)))

    _activate_clear_successor(
        staged,
        _handler(staged.sessions),
        successor_id,
        staged.resolution(),
        overrides={"expire_stale_terminal_sessions_for_context": MagicMock(side_effect=record)},
    )

    assert seen == [(staged.predecessor_id, "expired")]


def test_awaiting_handoff_row_survives_context_reuse_expiry(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A staged clear predecessor waits for its successor while other same-pane rows expire."""
    staged = _staged_clear(temp_db, tmp_path, monkeypatch, name="expiry-skip", pane="%103")
    bystander_id = staged.register("bystander-ext")
    newcomer_id = staged.register("newcomer-ext")

    expire_stale_terminal_sessions_for_context(
        _handler(staged.sessions),
        session_id=newcomer_id,
        project_id=staged.project_id,
        terminal_context=staged.term,
    )

    assert staged.status(bystander_id) == "expired"
    assert staged.status(staged.predecessor_id) == "awaiting_handoff"
    assert staged.status(newcomer_id) == "active"


def test_next_clear_takes_over_a_bound_but_unpulled_successor(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual /clear before get_handoff moves the marker, parent, and claims to the third row."""
    staged = _staged_clear(temp_db, tmp_path, monkeypatch, name="takeover", pane="%104")
    stale_id = staged.register("stale-ext")
    assert take_clear_handoff_marker(
        temp_db, staged.predecessor_id, attempt_id=_ATTEMPT_ID, successor_id=stale_id
    )
    staged.sessions.update_status_if_non_terminal(staged.predecessor_id, "expired")
    variables = SessionVariableManager(temp_db)
    variables.merge_variables(
        stale_id,
        {
            HANDOFF_PULL_PENDING_VARIABLE: True,
            "task_claimed": True,
            "claimed_tasks": {"task-1": "#1"},
        },
    )
    newcomer_id = staged.register("newcomer-ext")

    resolved = resolve_clear_continuation(
        temp_db,
        source="grok",
        project_id=staged.project_id,
        machine_id=staged.machine_id,
        terminal_context=staged.term,
        predecessor_hint=None,
    )
    assert resolved.predecessor is not None
    assert resolved.predecessor.id == staged.predecessor_id
    assert (resolved.attempt_id, resolved.supersedes) == (_ATTEMPT_ID, stale_id)

    handler = _handler(staged.sessions)
    preserve = MagicMock()
    _activate_clear_successor(
        staged,
        handler,
        newcomer_id,
        SessionStartResolution(
            session=None,
            session_source="clear",
            clear_predecessor=resolved.predecessor,
            clear_attempt_id=resolved.attempt_id,
            clear_supersedes=resolved.supersedes,
        ),
        overrides={
            "expire_stale_terminal_sessions_for_context": None,
            "preserve_task_claim_state": preserve,
        },
    )

    newcomer = staged.sessions.get(newcomer_id)
    assert newcomer is not None
    assert newcomer.parent_session_id == staged.predecessor_id
    marker = variables.get_variables(staged.predecessor_id)[CLEAR_ATTEMPT_VARIABLE]
    assert marker["consumed_by"] == newcomer_id
    assert variables.get_variables(newcomer_id)[HANDOFF_PULL_PENDING_VARIABLE] is True
    assert staged.status(stale_id) == "expired"
    assert staged.status(newcomer_id) == "active"
    moved = [(c.args[2], c.args[3]) for c in preserve.call_args_list]
    assert moved == [(newcomer_id, staged.predecessor_id), (newcomer_id, stale_id)]
    assert preserve.call_args_list[1].args[4]["claimed_tasks"] == {"task-1": "#1"}
    consumed = consume_pending_handoff(temp_db, newcomer_id)
    assert consumed is not None
    assert consumed.session_id == staged.predecessor_id

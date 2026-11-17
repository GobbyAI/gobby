"""Deferred first-activity materialization."""

from __future__ import annotations

import os
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import NAMESPACE_OID, uuid4, uuid5

import psutil
import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.event_handlers._session_start.claims import MCP_PROXY_READY_VARIABLE
from gobby.hooks.event_handlers._session_start.context import classify_session_start_context
from gobby.hooks.event_handlers._session_start.handoff import SessionStartResolution
from gobby.hooks.event_handlers._session_start.materialize import activate_materialized_session
from gobby.hooks.event_handlers._session_start.terminal_runtime import (
    expire_stale_terminal_sessions_for_context,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.session_materialize import activate_deferred_session, has_deferred_help_activation
from gobby.sessions.handoff import (
    HANDOFF_PULL_PENDING_VARIABLE,
    consume_pending_handoff,
)
from gobby.sessions.handoff_records import build_handoff_payload
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager, native_locator_key
from gobby.workflows.agent_resolver import AgentResolutionError
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import (
    IsolatedCheckoutFactory,
    install_isolated_checkout_project,
)
from tests.fixtures.legacy_handoff import seed_legacy_clear_handoff
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
    startup = HookResponse(system_message="Session identity")
    startup.add_context(("claimed_tasks", "Pending startup instructions"))
    manager._event_handlers._compose_session_response.return_value = startup
    prompt = ("$gobby" if source == SessionSource.CODEX else "/gobby") + suffix
    event = _event({"prompt": prompt}, source=source)
    state: dict[str, object] = {}

    def merge(session_id: str, values: dict[str, object]) -> None:
        state.update(values)

    with patch("gobby.hooks.session_materialize.SessionVariableManager") as variables:
        variables.return_value.get_variable_subset.return_value = state
        variables.return_value.merge_variables.side_effect = merge
        assert activate_deferred_session(manager, event, BlockingEffectDeadline(123.0)) is None
        assert not has_deferred_help_activation(manager, event)
        assert "_startup_context" not in event.metadata
        manager._event_handlers._activate_materialized_session.assert_not_called()
        manager._evaluate_workflow_rules.assert_not_called()

        event.data["prompt"] = "Implement the change"
        assert has_deferred_help_activation(manager, event)
        variables.return_value.get_variable_subset.assert_called_once_with(
            "platform-session", ("_help_deferred_activation",)
        )
        assert activate_deferred_session(manager, event, BlockingEffectDeadline(123.0)) is None
        assert event.metadata["_startup_context"] == [
            ("claimed_tasks", "Pending startup instructions")
        ]
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


def test_refused_agent_definition_still_stages_startup_and_commits_claim() -> None:
    session = SimpleNamespace(
        id="platform-session",
        project_id="project-1",
        parent_session_id=None,
        transcript_path="/repo/t.jsonl",
    )
    manager = _manager(session, None)
    manager._event_handlers._inject_agent_instructions_if_needed.side_effect = AgentResolutionError(
        "terminal_backend is no longer agent-authored"
    )
    event = _event({"cwd": "/repo", "transcript_path": "/repo/t.jsonl"})
    event.event_type = HookEventType.AFTER_TOOL
    event.metadata["_session_start_context_mode"] = "full"

    with patch("gobby.hooks.session_materialize.commit_stashed_startup_claim") as commit:
        assert activate_deferred_session(manager, event, BlockingEffectDeadline(123.0)) is None

    assert event.metadata["_startup_system_message"] == "Gobby Session ID: #7"
    commit.assert_called_once_with(manager._event_handlers, "platform-session", event.metadata)


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
    assert "resolution" not in activate


def test_deferred_activation_does_not_resume_a_clear_handoff() -> None:
    session = SimpleNamespace(
        id="platform-session",
        project_id="project-1",
        parent_session_id=None,
        transcript_path="/repo/t.jsonl",
    )
    manager = _manager(session, None)
    resolution = SessionStartResolution(
        session=None,
        session_source="clear",
    )
    event = _event(
        {
            "prompt": "hello",
            "cwd": "/repo",
            "transcript_path": "/repo/t.jsonl",
            "terminal_context": {"gobby_terminal_id": "960e3e05-1677-50aa-a5a8-bfef022d3b85"},
        }
    )

    with patch(
        "gobby.hooks.session_materialize.resolve_matching_clear_continuation",
        create=True,
        return_value=resolution,
    ) as mock_resolve:
        assert activate_deferred_session(manager, event, BlockingEffectDeadline(123.0)) is None

    mock_resolve.assert_not_called()
    activate = manager._event_handlers._activate_materialized_session.call_args.kwargs
    assert "resolution" not in activate


@patch(
    "gobby.hooks.event_handlers._session_start.materialize.classify_session_start_context",
    return_value=SimpleNamespace(mode="full"),
)
@patch(
    "gobby.hooks.event_handlers._session_start.materialize.expire_stale_terminal_sessions_for_context"
)
def test_startup_retires_stale_clear_without_inheriting_handoff(
    _mock_expire: MagicMock,
    _mock_classify: MagicMock,
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First activity creates an independent row despite a stale legacy clear."""
    checkout = install_isolated_checkout_project(
        temp_db, tmp_path / "clear-bind", name="clear-bind", monkeypatch=monkeypatch
    )
    machine_id = checkout.machine_id
    project = checkout.project
    root = checkout.root_path
    sessions = SessionManager(temp_db)
    term = {
        "gobby_terminal_id": "960e3e05-1677-50aa-a5a8-bfef022d3b85",
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
    seed_legacy_clear_handoff(
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
            "agent_name_override": "synthetic-old-persona",
            "parent_session_id": predecessor_id,
            "workflow_name": "synthetic-old-workflow",
            "cwd": root,
        }
    )
    event.task_id = "current-task"
    resolution = SessionStartResolution(
        session=None,
        session_source="startup",
    )

    with (
        patch("gobby.hooks.event_handlers._session_start.materialize._seed_parent_turn_seq"),
        patch("gobby.hooks.event_handlers._session_start.materialize.seed_user_profile_content"),
        patch(
            "gobby.hooks.event_handlers._session_start.materialize.prepare_compact_continuation_variables"
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
    assert rebound.parent_session_id is None
    retired = sessions.get(predecessor_id)
    assert retired is not None and retired.status == "expired"
    consumed = consume_pending_handoff(temp_db, successor_id)
    assert consumed is None
    handler._activate_default_agent.assert_called_once_with(
        successor_id, "grok", project.id, agent_name_override=None
    )
    assert event.task_id == "current-task"
    assert "workflow_name" not in event.data
    assert "parent_session_id" not in event.data
    # Only a /clear start was submitted from the composer.
    handler._record_composer_submit.assert_not_called()


@pytest.mark.parametrize("cli", [SessionSource.GROK, SessionSource.CODEX])
def test_fresh_clear_never_schedules_a_handoff_prompt(
    cli: SessionSource,
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh provider conversation never receives the old continuation prompt."""
    checkout = install_isolated_checkout_project(
        temp_db, tmp_path / f"clear-{cli.value}", name=f"clear-{cli.value}", monkeypatch=monkeypatch
    )
    machine_id = checkout.machine_id
    project = checkout.project
    root = checkout.root_path
    sessions = SessionManager(temp_db)
    term = {
        "gobby_terminal_id": "454eebf5-bba3-5596-862c-5742736f8466",
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
    seed_legacy_clear_handoff(
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
    )
    materialize = "gobby.hooks.event_handlers._session_start.materialize"

    with (
        patch(f"{materialize}._seed_parent_turn_seq"),
        patch(f"{materialize}.seed_user_profile_content"),
        patch(f"{materialize}.prepare_compact_continuation_variables"),
        patch(f"{materialize}.classify_session_start_context"),
        patch(f"{materialize}.expire_stale_terminal_sessions_for_context"),
        patch(f"{materialize}.schedule_handoff_continuation", create=True) as mock_schedule,
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
    assert rebound.parent_session_id is None
    assert consume_pending_handoff(temp_db, successor_id) is None
    mock_schedule.assert_not_called()


_MATERIALIZE = "gobby.hooks.event_handlers._session_start.materialize"
# Activation side effects every clear-bind test below stubs unless a test names
# one in ``overrides`` (``None`` keeps the real function).
_ACTIVATION_STUBS = (
    "_seed_parent_turn_seq",
    "seed_user_profile_content",
    "prepare_compact_continuation_variables",
    "classify_session_start_context",
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
        )


def _staged_clear(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    pane: str,
    native: bool = False,
) -> _StagedClear:
    checkout = install_isolated_checkout_project(
        temp_db, tmp_path / name, name=name, monkeypatch=monkeypatch
    )
    if native:
        host_epoch = f"epoch-{name}"
        host_terminal_id = f"host-{name}"
        terminals = TerminalManager(temp_db)
        terminal_id = str(uuid4())
        pending = terminals.create_pending(
            terminal_id=terminal_id,
            machine_id=checkout.machine_id,
            project_id=checkout.project.id,
            backend="native",
            ownership="gobby",
            spawn_key=terminal_id,
        )
        assert pending.attempt_started_at is not None
        assert (
            terminals.record_process(
                terminal_id,
                {"pgid": os.getpid(), "host_terminal_id": host_terminal_id},
                attempt_generation=pending.attempt_generation,
                attempt_started_at=pending.attempt_started_at,
            )
            is not None
        )
        terminal = terminals.promote_to_live(
            terminal_id,
            locator={"host_terminal_id": host_terminal_id},
            locator_key=native_locator_key(host_epoch, host_terminal_id),
            host_epoch=host_epoch,
        )
        assert terminal is not None
        term: dict[str, object] = {
            "gobby_terminal_id": terminal.id,
            "host_terminal_id": host_terminal_id,
            "parent_pid": os.getpid(),
            "parent_create_time": psutil.Process().create_time(),
        }
    else:
        term = {
            "gobby_terminal_id": str(uuid5(NAMESPACE_OID, pane)),
            "parent_pid": 10324,
            "parent_create_time": 1.0,
        }
    staged = _StagedClear(
        sessions=SessionManager(temp_db),
        machine_id=checkout.machine_id,
        project_id=checkout.project.id,
        root=checkout.root_path,
        term=term,
    )
    staged.predecessor_id = staged.register("pred-ext")
    seed_legacy_clear_handoff(
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


def test_context_reuse_expiry_runs_after_the_previous_session_retires(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expiry sees an independent newcomer after the old conversation retires."""
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

    assert seen == [(None, "expired")]


def test_native_bind_retries_after_clear_predecessor_expiry(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _staged_clear(
        temp_db,
        tmp_path,
        monkeypatch,
        name="native-rebind",
        pane="",
        native=True,
    )
    terminals = TerminalManager(temp_db)
    terminal_id = staged.term["gobby_terminal_id"]
    assert isinstance(terminal_id, str)
    holder_id = staged.register("native-holder-ext")
    assert terminals.bind_session(terminal_id, holder_id, staged.project_id) is not None
    successor_id = staged.register("native-successor-ext")
    handler = _handler(staged.sessions)
    handler.terminal_manager = terminals

    with patch.object(psutil.Process, "terminal", return_value="/dev/ttys001"):
        _activate_clear_successor(
            staged,
            handler,
            successor_id,
            staged.resolution(),
            overrides={"expire_stale_terminal_sessions_for_context": None},
        )

    assert staged.status(holder_id) == "expired"
    rebound = terminals.get_live_for_session(successor_id)
    assert rebound is not None
    assert rebound.id == terminal_id
    # The /clear submit is recorded for the successor once it holds the pane.
    handler._record_composer_submit.assert_called_once_with(successor_id)


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


def test_context_reuse_expiry_keeps_same_tty_on_another_machine(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY names are machine-local; only a local reused tty retires its row."""
    staged = _staged_clear(temp_db, tmp_path, monkeypatch, name="machine-expiry", pane="%104")
    staged.term = {"tty": "/dev/ttys001", "parent_pid": 111, "parent_create_time": 1.0}
    local_id = staged.register("local-tty")
    machines = LocalMachineManager(temp_db)
    local_machine = machines.get(staged.machine_id)
    assert local_machine is not None
    other_machine = machines.upsert_seen(str(uuid4()), local_machine.owner_user_id)
    with patch(
        "gobby.storage.workspace_machine_scope.require_machine_id", return_value=other_machine.id
    ):
        remote_id = staged.sessions.register_session(
            external_id="remote-tty",
            machine_id=other_machine.id,
            source="grok",
            project_id=staged.project_id,
            terminal_context=staged.term,
        )
    staged.term = {"tty": "/dev/ttys001", "parent_pid": 222, "parent_create_time": 2.0}
    current_id = staged.register("current-tty")

    expire_stale_terminal_sessions_for_context(
        _handler(staged.sessions),
        session_id=current_id,
        project_id=staged.project_id,
        terminal_context=staged.term,
    )

    assert staged.status(remote_id) == "active"
    assert staged.status(local_id) == "expired"
    assert staged.status(current_id) == "active"


@pytest.mark.parametrize("predecessor_ready", [True, False])
def test_fresh_session_requires_its_own_proxy_readiness(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    predecessor_ready: bool,
) -> None:
    """A new session establishes its own proxy credential and readiness."""
    staged = _staged_clear(
        temp_db, tmp_path, monkeypatch, name=f"ready-{predecessor_ready}", pane="%105"
    )
    variables = SessionVariableManager(temp_db)
    if predecessor_ready:
        variables.merge_variables(staged.predecessor_id, {MCP_PROXY_READY_VARIABLE: True})
    successor_id = staged.register("successor-ext")

    _activate_clear_successor(staged, _handler(staged.sessions), successor_id, staged.resolution())

    successor_vars = variables.get_variables(successor_id)
    assert successor_vars.get(HANDOFF_PULL_PENDING_VARIABLE) is not True
    assert successor_vars.get(MCP_PROXY_READY_VARIABLE) is None

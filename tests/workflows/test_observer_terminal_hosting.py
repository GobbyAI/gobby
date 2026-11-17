"""Hosting follows the durable native route, refreshed at turn boundaries."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from gobby.hooks.event_handlers._session_start.terminal_runtime import bind_discovered_terminal
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.workflows._variables import set_variable
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager, native_locator_key
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.observer_dispatch import run_observers
from gobby.workflows.observer_terminal_hosting import detect_native_terminal_hosting

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("transition", ["exit", "tty_mismatch"])
def test_hosting_follows_live_native_terminal_row(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    transition: str,
) -> None:
    session = session_manager.register(
        external_id="hosting-test",
        machine_id=require_machine_id(),
        source="claude",
        project_id=sample_project["id"],
    )
    terminals = TerminalManager(temp_db)
    handler = SimpleNamespace(_session_manager=session_manager, terminal_manager=terminals)
    variables: dict[str, Any] = {}
    detect_native_terminal_hosting(handler, session.id, variables)
    assert variables["native_terminal_hosted"] is False
    tid, host_id, epoch = (str(uuid4()) for _ in range(3))
    row = terminals.create_pending(
        terminal_id=tid,
        project_id=sample_project["id"],
        backend="native",
        ownership="gobby",
        spawn_key=tid,
        session_id=session.id,
    )
    live = terminals.promote_to_live(
        row.id,
        locator={"host_terminal_id": host_id},
        locator_key=native_locator_key(epoch, host_id),
        host_epoch=epoch,
    )
    assert live is not None
    detect_native_terminal_hosting(handler, session.id, variables)
    assert variables["native_terminal_hosted"] is True
    if transition == "exit":
        terminals.mark_exited(tid)
    else:
        context: dict[str, Any] = {"gobby_terminal_id": tid, "parent_pid": 202}
        with patch(
            "gobby.hooks.event_handlers._session_start.terminal_runtime.cli_shares_shell_tty",
            return_value=False,
        ):
            bind_discovered_terminal(
                terminals,
                MagicMock(),
                session_id=session.id,
                project_id=session.project_id,
                terminal_context=context,
                session_manager=session_manager,
            )
        assert "gobby_terminal_id" not in context
        released = terminals.get(tid)
        assert released is not None and released.session_id is None
    detect_native_terminal_hosting(handler, session.id, variables)
    assert variables["native_terminal_hosted"] is False


@pytest.mark.parametrize("session_type", ["web_chat", None])
def test_nonterminal_or_missing_session_leaves_hosting_unset(session_type: str | None) -> None:
    sessions, terminals = MagicMock(), MagicMock()
    sessions.get.return_value = (
        None if session_type is None else SimpleNamespace(session_type=session_type)
    )
    variables: dict[str, Any] = {"native_terminal_hosted": True}
    detect_native_terminal_hosting(
        SimpleNamespace(_session_manager=sessions, terminal_manager=terminals), "session", variables
    )
    assert "native_terminal_hosted" not in variables
    terminals.resolve_live_for_session.assert_not_called()


def test_missing_terminal_manager_leaves_hosting_unset() -> None:
    sessions = MagicMock()
    variables: dict[str, Any] = {"native_terminal_hosted": False}
    detect_native_terminal_hosting(SimpleNamespace(_session_manager=sessions), "session", variables)
    assert "native_terminal_hosted" not in variables
    sessions.get.assert_not_called()


@pytest.mark.parametrize(
    ("event_type", "expected_calls"),
    [
        (HookEventType.SESSION_START, 1),
        (HookEventType.BEFORE_AGENT, 1),
        (HookEventType.AFTER_TOOL, 0),
    ],
)
def test_dispatch_refreshes_hosting_before_pressure(
    event_type: HookEventType, expected_calls: int
) -> None:
    handler = MagicMock()
    session = SimpleNamespace(session_type="terminal", context_used_tokens=400_000)
    handler._session_manager.get.return_value = session
    handler.terminal_manager.resolve_live_for_session.return_value = None
    handler._config_section.return_value = None
    event = HookEvent(
        event_type=event_type,
        session_id="session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
    )
    variables: dict[str, Any] = {}
    failures = run_observers(handler, event, "session", variables)
    assert "detect_native_terminal_hosting" not in failures
    assert handler.terminal_manager.resolve_live_for_session.call_count == expected_calls
    if expected_calls:
        assert variables["native_terminal_hosted"] is False
        assert not variables.get("context_compact_guidance_message")


@pytest.mark.parametrize("value", [None, True, False])
def test_hosting_variable_is_reserved(value: bool | None) -> None:
    sessions, variables = MagicMock(), MagicMock()
    with (
        patch(
            "gobby.mcp_proxy.tools.workflows._variables.resolve_session_id", return_value="session"
        ),
        patch("gobby.workflows.state_manager.SessionVariableManager") as authority,
    ):
        authority.return_value.get_variable_subset.return_value = {"_agent_type": "default"}
        result = set_variable(
            sessions,
            MagicMock(),
            "native_terminal_hosted",
            value,
            "session",
            session_var_manager=variables,
        )
    assert result["success"] is False
    assert "reserved variable" in result["error"]
    variables.set_variable.assert_not_called()

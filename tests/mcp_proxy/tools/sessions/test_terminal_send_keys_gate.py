"""The target checks an allowed ``send_keys`` caller must pass before any key is written."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import _terminal_send_keys_gate as gate
from gobby.mcp_proxy.tools.sessions._terminal_send_keys import register_send_keys_tool
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.terminals.runtime import Delivered
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

SESSION_ID = "33333333-3333-4333-8333-333333333333"
TERMINAL_ID = "terminal-1"


class _Manager:
    def __init__(self, terminal: object | None, error: Exception | None) -> None:
        self._terminal = terminal
        self._error = error

    def get_live_for_session(self, session_id: str) -> object | None:
        if self._error is not None:
            raise self._error
        return self._terminal

    def resolve_live_for_session(self, session: object) -> object | None:
        return self._terminal


class _Pane:
    def __init__(self, text: str | None) -> None:
        self._text = text

    async def snapshot(self, lines: int = 80, *, mode: str = "text") -> str | None:
        return self._text


def _terminal(pgid: int | None = 4321) -> SimpleNamespace:
    return SimpleNamespace(id=TERMINAL_ID, process={} if pgid is None else {"pgid": pgid})


def _scene(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> _Manager:
    """Install an idle agent CLI in the foreground, then apply ``overrides``."""
    scene: dict[str, Any] = {
        "terminal": _terminal(),
        "foreground": {TERMINAL_ID: "claude"},
        "composer": "empty",
        "snapshot": "> ",
        "pane": True,
        "lookup_error": None,
    } | overrides
    pane = _Pane(scene["snapshot"]) if scene["pane"] else None
    monkeypatch.setattr(gate, "foreground_commands", lambda _pids: scene["foreground"])
    monkeypatch.setattr(gate, "read_composer", lambda _id: SimpleNamespace(state=scene["composer"]))
    monkeypatch.setattr(gate, "live_runtime_pane", lambda *_args: pane)
    return _Manager(scene["terminal"], scene["lookup_error"])


REFUSALS = [
    pytest.param({"terminal": None}, "send_keys_no_live_terminal", id="no-live-terminal"),
    pytest.param({"terminal": _terminal(pgid=None)}, "send_keys_target_unconfirmed", id="tmux-pid"),
    pytest.param({"foreground": {}}, "send_keys_target_unconfirmed", id="foreground-unread"),
    pytest.param(
        {"foreground": {TERMINAL_ID: "zsh"}}, "send_keys_target_bare_shell", id="bare-shell"
    ),
    pytest.param({"composer": "draft"}, "send_keys_composer_not_idle", id="human-draft"),
    pytest.param({"composer": "held"}, "send_keys_composer_not_idle", id="daemon-held-text"),
    pytest.param({"composer": "unknown"}, "send_keys_composer_not_idle", id="untracked"),
    pytest.param({"pane": False}, "send_keys_target_unconfirmed", id="pane-missing"),
    pytest.param({"snapshot": None}, "send_keys_target_unconfirmed", id="snapshot-unread"),
    pytest.param({"snapshot": ""}, "send_keys_target_unconfirmed", id="empty-pane"),
    pytest.param({"snapshot": " \n\t"}, "send_keys_target_unconfirmed", id="blank-pane"),
    pytest.param(
        {"foreground": {TERMINAL_ID: "vim"}},
        "send_keys_target_unconfirmed",
        id="non-agent-foreground",
    ),
    pytest.param(
        {"foreground": {TERMINAL_ID: "python"}, "snapshot": "> "},
        "send_keys_target_unconfirmed",
        id="exited-cli-stale-composer",
    ),
    pytest.param({"snapshot": "Password:"}, "send_keys_credential_prompt", id="password"),
    pytest.param(
        {"snapshot": "[sudo] password for josh:"}, "send_keys_credential_prompt", id="sudo"
    ),
    pytest.param(
        {"snapshot": "Enter passphrase for key '/home/josh/.ssh/id':"},
        "send_keys_credential_prompt",
        id="passphrase",
    ),
    pytest.param(
        {"lookup_error": RuntimeError("terminal table unavailable")},
        "send_keys_target_unconfirmed",
        id="lookup-raises",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("overrides", "code"), REFUSALS)
async def test_refuses_a_target_that_cannot_take_keys(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], code: str
) -> None:
    manager = _scene(monkeypatch, **overrides)

    refusal = await gate.send_keys_target_refusal(SESSION_ID, manager, object())

    assert refusal is not None
    assert refusal.code == code
    assert "gobby-agents:send_message" in refusal.message


@pytest.mark.asyncio
async def test_admits_a_target_showing_an_idle_agent_composer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _scene(monkeypatch)

    assert await gate.send_keys_target_refusal(SESSION_ID, manager, object()) is None


@pytest.mark.asyncio
async def test_a_password_line_above_an_idle_prompt_is_not_a_credential_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _scene(monkeypatch, snapshot="Password: \n> ")

    assert await gate.send_keys_target_refusal(SESSION_ID, manager, object()) is None


@pytest.mark.asyncio
async def test_missing_manager_or_registry_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _scene(monkeypatch)

    no_manager = await gate.send_keys_target_refusal(SESSION_ID, None, object())
    no_registry = await gate.send_keys_target_refusal(SESSION_ID, _Manager(_terminal(), None), None)

    assert no_manager is not None and no_manager.code == "send_keys_target_unconfirmed"
    assert no_registry is not None and no_registry.code == "send_keys_target_unconfirmed"


@pytest.fixture
def public_send_keys(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> tuple[Callable[..., Any], AsyncMock, str]:
    """Register the public handler with a real caller and an isolated write sink."""
    operator = getattr(request, "param", "capable") == "operator"
    caller = session_manager.register(
        external_id="send-keys-public-caller",
        machine_id=require_machine_id(),
        source="claude",
        project_id=sample_project["id"],
        agent_depth=0 if operator else 1,
    )
    if not operator:
        SessionVariableManager(temp_db).set_variable(caller.id, "_agent_type", "orchestrator")
    target = session_manager.register(
        external_id="send-keys-public-target",
        machine_id=require_machine_id(),
        source="claude",
        project_id=sample_project["id"],
    )
    monkeypatch.setattr("gobby.utils.session_context.get_current_session_id", lambda: caller.id)
    registry = InternalToolRegistry(name="test-send-keys")
    writer = AsyncMock(return_value=Delivered())

    def register(manager: _Manager) -> Callable[..., Any]:
        register_send_keys_tool(
            registry,
            session_manager,
            terminal_manager=manager,
            terminal_runtime_registry=object(),
            write_coordinator=SimpleNamespace(write=writer),
        )
        handler = registry.get_tool("send_keys")
        assert handler is not None
        return handler

    return register, writer, target.id


@pytest.mark.asyncio
@pytest.mark.parametrize(("overrides", "code"), REFUSALS)
async def test_public_send_keys_refusal_never_writes(
    monkeypatch: pytest.MonkeyPatch,
    public_send_keys: tuple[Callable[..., Any], AsyncMock, str],
    overrides: dict[str, Any],
    code: str,
) -> None:
    register, writer, target_id = public_send_keys
    handler = register(_scene(monkeypatch, **overrides))

    result = await handler(session_id=target_id, keys="resume\n")

    assert result["success"] is False
    assert result["error_code"] == code
    assert "gobby-agents:send_message" in result["error"]
    writer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("public_send_keys", ["capable", "operator"], indirect=True)
async def test_public_send_keys_delivers_to_idle_cli_composer(
    monkeypatch: pytest.MonkeyPatch,
    public_send_keys: tuple[Callable[..., Any], AsyncMock, str],
) -> None:
    register, writer, target_id = public_send_keys
    handler = register(_scene(monkeypatch))

    result = await handler(session_id=target_id, keys="resume\n")

    assert result["success"] is True
    writer.assert_awaited_once()
    write = writer.await_args
    assert write is not None
    assert write.args[0].terminal_id == TERMINAL_ID
    assert write.args[0].payload == "resume"
    assert write.args[0].submit is True


@pytest.mark.asyncio
@pytest.mark.parametrize("public_send_keys", ["operator"], indirect=True)
async def test_public_send_keys_applies_the_composer_gate_to_the_operator(
    monkeypatch: pytest.MonkeyPatch,
    public_send_keys: tuple[Callable[..., Any], AsyncMock, str],
) -> None:
    register, writer, target_id = public_send_keys
    handler = register(_scene(monkeypatch, foreground={TERMINAL_ID: "zsh"}))

    result = await handler(session_id=target_id, keys="echo operator\n")

    assert result["success"] is False
    assert result["error_code"] == "send_keys_target_bare_shell"
    assert "gobby-agents:send_message" in result["error"]
    writer.assert_not_awaited()

"""The target checks an allowed ``send_keys`` caller must pass before any key is written."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gobby.mcp_proxy.tools.sessions import _terminal_send_keys_gate as gate

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

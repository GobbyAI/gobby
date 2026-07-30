from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.servers.websocket.handlers.session_observe_support import (
    _can_proxy_attach_session,
)


@pytest.mark.parametrize("status", ["active", "paused", "handoff_ready"])
def test_eligible_tmux_session_can_proxy_attach(status: str) -> None:
    session = SimpleNamespace(
        session_type="terminal",
        status=status,
        terminal_context={"tmux_pane": "%1"},
    )

    assert _can_proxy_attach_session(session) is True


@pytest.mark.parametrize("status", ["expired", "deleted"])
def test_inactive_session_cannot_proxy_attach_even_with_explicit_flag(status: str) -> None:
    session = SimpleNamespace(
        session_type="terminal",
        status=status,
        terminal_context={"tmux_pane": "%1"},
        can_proxy_attach=True,
    )

    assert _can_proxy_attach_session(session) is False

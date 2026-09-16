"""TmuxPTYBridge registration invariants."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

from gobby.agents.tmux.pty_bridge import TmuxPTYBridge

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_attach_refuses_an_empty_terminal_id() -> None:
    """A bridge without its terminals row would emit frames the client drops."""
    bridge = TmuxPTYBridge()
    with pytest.raises(ValueError, match="terminal_id is required"):
        await bridge.attach("gobby-demo", "stream-1", terminal_id="")
    # The refusal happens before registration, so nothing is left pending.
    assert await bridge.get_bridge("stream-1") is None


@pytest.mark.asyncio
async def test_attach_refuses_on_windows_before_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows has no fcntl/termios, so attach refuses before any PTY syscall."""
    bridge = TmuxPTYBridge()
    openpty = MagicMock()
    monkeypatch.setattr(os, "openpty", openpty)
    with monkeypatch.context() as platform_patch:
        platform_patch.setattr(sys, "platform", "win32")
        with pytest.raises(RuntimeError, match="requires a POSIX platform"):
            await bridge.attach("gobby-demo", "stream-1", terminal_id="terminal-1")

    openpty.assert_not_called()
    # The refusal precedes pending registration, so the streaming id stays reusable.
    assert "stream-1" not in bridge._pending_bridges
    assert await bridge.get_bridge("stream-1") is None

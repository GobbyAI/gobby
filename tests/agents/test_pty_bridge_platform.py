"""Platform guards for the tmux PTY bridge.

``fcntl``/``termios`` are POSIX-only. Importing them at module scope made the
whole ``gobby.agents.tmux`` package — and therefore daemon startup — fail on
Windows. The module must import everywhere and degrade at call time instead.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from gobby.agents.tmux import pty_bridge
from gobby.agents.tmux.pty_bridge import TmuxPTYBridge

pytestmark = pytest.mark.unit


class TestPtyBridgePlatformGuards:
    def test_module_exposes_support_flag(self) -> None:
        """Importing the module must never raise, on any platform."""
        assert isinstance(pty_bridge.PTY_SUPPORTED, bool)

    async def test_attach_raises_clear_error_when_unsupported(self) -> None:
        """attach() should explain the platform limit, not AttributeError."""
        bridge = TmuxPTYBridge()

        with patch.object(pty_bridge, "PTY_SUPPORTED", False):
            with pytest.raises(RuntimeError, match="POSIX"):
                await bridge.attach("session", "streaming-id")

    async def test_attach_does_not_open_a_pty_when_unsupported(self) -> None:
        """The guard must fire before any POSIX-only syscall."""
        bridge = TmuxPTYBridge()

        with (
            patch.object(pty_bridge, "PTY_SUPPORTED", False),
            # create=True: os.openpty does not exist on Windows, which is the
            # very platform this guard protects.
            patch("os.openpty", create=True) as mock_openpty,
        ):
            with pytest.raises(RuntimeError):
                await bridge.attach("session", "streaming-id")

        mock_openpty.assert_not_called()

    async def test_resize_returns_none_when_unsupported(self) -> None:
        """resize() already returns None on failure; keep that contract."""
        bridge = TmuxPTYBridge()

        with patch.object(pty_bridge, "PTY_SUPPORTED", False):
            assert await bridge.resize("missing-id", 24, 80) is None

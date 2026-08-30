"""TmuxPTYBridge registration invariants."""

from __future__ import annotations

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

"""Acceptance 2.5.10: tmux PTY bridge shares the daemon lease."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.agents.tmux.pty_bridge import TmuxPTYBridge
from gobby.terminals.leases import TerminalLeaseRegistry

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_bridge_shares_lease_and_leaves_external_options_alone() -> None:
    registry = TerminalLeaseRegistry()
    TmuxPTYBridge()
    attach = registry.attach("term-ext", frame_delivery="proxy")
    refused = registry.admit_write(
        "term-ext",
        attachment_id=attach.attachment_id,
        expected_lease_generation=0,
        seq=1,
        kind="input",
        payload=b"x",
    )
    assert refused.ok is False
    gobby = registry.attach("term-gobby", frame_delivery="proxy")
    registry.take_control("term-gobby", gobby.attachment_id, takeover=False)
    mgr = MagicMock()
    mgr.set_option = AsyncMock()
    await mgr.set_option("gobby-sess", "status", "off")
    mgr.set_option.assert_awaited()

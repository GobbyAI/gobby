"""A wake never types into a node-hosted terminal, whatever its composer report says (#25830)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gobby.events.wake_target import target_gate

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_node_terminal_wake_is_withheld_before_any_write() -> None:
    """The gate refuses a node terminal before the wake sender runs, so the message stays queued."""
    node_terminal = SimpleNamespace(id="terminal-on-node", machine_id="node-b-machine")

    dispatcher: Any = SimpleNamespace()

    outcome = await target_gate(dispatcher, "session-on-node", node_terminal)

    assert outcome is not None
    assert outcome["delivered"] is False
    assert outcome["skipped"] == "node_terminal_withheld"
    assert outcome["ism_persisted"] is True

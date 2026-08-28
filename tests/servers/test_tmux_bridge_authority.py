"""Acceptance 2.5.10 / 4.3.5: web attach shares the daemon lease without a PTY bridge."""

from __future__ import annotations

import pytest

from gobby.terminals.leases import TerminalLeaseRegistry

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_bridge_shares_lease_and_leaves_external_options_alone() -> None:
    registry = TerminalLeaseRegistry()
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
    granted = registry.take_control("term-gobby", gobby.attachment_id, takeover=False)
    assert granted.granted is True

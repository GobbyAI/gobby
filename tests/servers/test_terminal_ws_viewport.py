"""Acceptance 2.5.14 / 2.5.33: attachment-local viewport and scroll offset."""

from __future__ import annotations

import pytest

from gobby.terminals.leases import TerminalLeaseRegistry

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_two_observers_independent_viewports() -> None:
    registry = TerminalLeaseRegistry()
    a = await registry.attach("t", frame_delivery="proxy")
    b = await registry.attach("t", frame_delivery="proxy")
    registry.set_viewport(a.attachment_id, rows=24, cols=80)
    registry.set_viewport(b.attachment_id, rows=40, cols=120)
    assert registry.viewport(a.attachment_id) == (24, 80)
    assert registry.viewport(b.attachment_id) == (40, 120)
    holder = await registry.attach("t", frame_delivery="proxy")
    await registry.take_control("t", holder.attachment_id, takeover=False)
    resized = registry.resize_pty(a.attachment_id, rows=10, cols=10)
    assert resized.ok is True
    assert resized.applied is True


@pytest.mark.asyncio
async def test_two_observers_independent_scroll_offsets() -> None:
    registry = TerminalLeaseRegistry()
    a = await registry.attach("t", frame_delivery="proxy")
    b = await registry.attach("t", frame_delivery="proxy")
    applied_a = registry.set_scroll_offset(a.attachment_id, rows_from_live_edge=12, max_rows=40)
    applied_b = registry.set_scroll_offset(b.attachment_id, rows_from_live_edge=0, max_rows=40)
    assert applied_a.applied_rows == 12
    assert applied_b.applied_rows == 0
    assert registry.scroll_offset(a.attachment_id) != registry.scroll_offset(b.attachment_id)

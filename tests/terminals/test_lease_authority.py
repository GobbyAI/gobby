"""Acceptance 2.5.12: daemon-held lease is the only grant point."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.ws_protocol import TERMINAL_WS_SAFE_INTEGER_MAX

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_single_grant_point_across_all_paths() -> None:
    registry = TerminalLeaseRegistry()
    first = await registry.attach("term-1", frame_delivery="proxy")
    granted = await registry.take_control("term-1", first.attachment_id, takeover=False)
    assert granted.granted is True
    second = await registry.attach("term-1", frame_delivery="direct")
    held = await registry.take_control("term-1", second.attachment_id, takeover=False)
    assert held.granted is False
    assert held.reason == "held"
    takeover = await registry.take_control("term-1", second.attachment_id, takeover=True)
    assert takeover.granted is True
    assert takeover.lease_generation > granted.lease_generation
    assert registry.holder("term-1") == second.attachment_id
    assert _lease_replicas() == [], f"lease replicas exist: {_lease_replicas()}"


@pytest.mark.asyncio
async def test_sizing_owner_follows_viewer_precedence() -> None:
    registry = TerminalLeaseRegistry()
    web = await registry.attach("term-1", viewer="web")
    gclient = await registry.attach("term-1", viewer="gclient")
    await registry.take_control("term-1", web.attachment_id)

    web_resize = registry.resize_pty(web.attachment_id, rows=24, cols=80)
    gclient_resize = registry.resize_pty(gclient.attachment_id, rows=40, cols=120)

    assert web_resize.ok is True
    assert web_resize.applied is True
    assert gclient_resize.ok is True
    assert gclient_resize.applied is False
    assert gclient_resize.owner_viewer == "web"

    finalized = await registry.finalize(web.attachment_id, reason="detach")
    assert finalized is not None
    assert finalized.sizing is not None
    assert finalized.sizing.owner_viewer == "gclient"
    assert (finalized.sizing.rows, finalized.sizing.cols) == (40, 120)


@pytest.mark.asyncio
async def test_web_viewer_sizes_only_while_holding_the_input_lease() -> None:
    registry = TerminalLeaseRegistry()
    web = await registry.attach("term-1", viewer="web")
    gclient = await registry.attach("term-1", viewer="gclient")
    assert registry.resize_pty(gclient.attachment_id, rows=40, cols=120).applied is True

    watching = registry.resize_pty(web.attachment_id, rows=24, cols=80)
    assert watching.ok is True
    assert watching.applied is False
    assert watching.owner_viewer == "gclient"

    typing = await registry.take_control("term-1", web.attachment_id)
    assert typing.sizing is not None
    assert typing.sizing.owner_viewer == "web"
    assert (typing.sizing.rows, typing.sizing.cols) == (24, 80)

    released = await registry.release_control(web.attachment_id)
    assert released.sizing is not None
    assert released.sizing.owner_viewer == "gclient"
    assert (released.sizing.rows, released.sizing.cols) == (40, 120)


@pytest.mark.asyncio
async def test_releasing_the_only_sizing_web_viewer_unpins_the_terminal() -> None:
    registry = TerminalLeaseRegistry()
    web = await registry.attach("term-1", viewer="web", backend="tmux")
    await registry.take_control("term-1", web.attachment_id)
    assert registry.resize_pty(web.attachment_id, rows=24, cols=80).applied is True

    released = await registry.release_control(web.attachment_id)

    assert released.sizing is not None
    assert released.sizing.owner_viewer is None
    assert released.sizing.applied is False


@pytest.mark.asyncio
async def test_tmux_web_watcher_without_lease_does_not_size() -> None:
    # A tmux window has the human's own client on it; a watching phone must
    # not pin that client to its grid.
    registry = TerminalLeaseRegistry()
    phone = await registry.attach("term-1", viewer="web", backend="tmux")

    watching = registry.resize_pty(phone.attachment_id, rows=38, cols=50)

    assert watching.ok is True
    assert watching.applied is False
    assert watching.owner_viewer is None


@pytest.mark.asyncio
async def test_geometry_less_gclient_seat_does_not_block_unattended_web_sizing() -> None:
    # A gclient attachment that never declared a grid has nothing to protect,
    # so the native web watcher still sizes the terminal until that seat
    # reports its own geometry.
    registry = TerminalLeaseRegistry()
    seat = await registry.attach("term-1", viewer="gclient", backend="native")
    web = await registry.attach("term-1", viewer="web", backend="native")

    unattended = registry.resize_pty(web.attachment_id, rows=40, cols=151)
    assert unattended.applied is True
    assert unattended.sizing is not None
    assert unattended.sizing.owner_viewer == "web"
    assert (unattended.sizing.rows, unattended.sizing.cols) == (40, 151)

    seated = registry.resize_pty(seat.attachment_id, rows=24, cols=80)
    assert seated.applied is True
    assert seated.owner_viewer == "gclient"
    assert registry.resize_pty(web.attachment_id, rows=40, cols=151).applied is False


@pytest.mark.asyncio
async def test_native_web_watchers_size_an_unattended_terminal() -> None:
    # An agent's native terminal has no gclient seat and no lease holder, so
    # the web viewers watching it are its only geometry: the latest resize
    # wins, and a gclient seat takes sizing back the moment it arrives.
    registry = TerminalLeaseRegistry()
    desktop = await registry.attach("term-1", viewer="web", backend="native")

    alone = registry.resize_pty(desktop.attachment_id, rows=40, cols=151)
    assert alone.applied is True
    assert alone.sizing is not None
    assert alone.sizing.owner_viewer == "web"
    assert (alone.sizing.rows, alone.sizing.cols) == (40, 151)

    phone = await registry.attach("term-1", viewer="web", backend="native")
    latest = registry.resize_pty(phone.attachment_id, rows=38, cols=50)
    assert latest.applied is True
    assert latest.sizing is not None
    assert (latest.sizing.rows, latest.sizing.cols) == (38, 50)
    assert registry.resize_pty(desktop.attachment_id, rows=40, cols=151).applied is True

    gclient = await registry.attach("term-1", viewer="gclient", backend="native")
    seated = registry.resize_pty(gclient.attachment_id, rows=24, cols=80)
    assert seated.applied is True
    assert seated.owner_viewer == "gclient"
    watching = registry.resize_pty(phone.attachment_id, rows=38, cols=50)
    assert watching.applied is False
    assert watching.owner_viewer == "gclient"

    left = await registry.finalize(gclient.attachment_id, reason="detach")
    assert left is not None
    assert left.sizing is not None
    assert left.sizing.owner_viewer == "web"
    assert (left.sizing.rows, left.sizing.cols) == (38, 50)


@pytest.mark.asyncio
async def test_lock_cells_are_refcounted() -> None:
    registry = TerminalLeaseRegistry()
    owner_entered = asyncio.Event()
    release_owner = asyncio.Event()
    waiter_entered = asyncio.Event()

    async def owner() -> None:
        async with registry.lock("term-1"):
            owner_entered.set()
            await release_owner.wait()

    async def waiter() -> None:
        async with registry.lock("term-1"):
            waiter_entered.set()

    owner_task = asyncio.create_task(owner())
    await owner_entered.wait()
    cell = registry._lock_cells["term-1"]
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert registry._lock_cells["term-1"] is cell
    assert cell.references == 2
    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task
    assert registry._lock_cells["term-1"] is cell
    assert cell.references == 1
    release_owner.set()
    await owner_task
    assert "term-1" not in registry._lock_cells
    assert not waiter_entered.is_set()


def test_lifecycle_sequence_rotates_epoch_before_overflow() -> None:
    registry = TerminalLeaseRegistry()
    previous_epoch = registry.daemon_epoch
    registry._lifecycle_seq = TERMINAL_WS_SAFE_INTEGER_MAX

    assert registry.next_lifecycle_seq() == 1
    assert registry.daemon_epoch != previous_epoch


# Lease bookkeeping a replica of the terminal grant point would carry.
LEASE_FIELDS = ("lease_token", "lease_ttl", "lease_expiry")

# The terminal-lease grant surface. A replica has to name what it grants
# control of, so it names an attachment. Other subsystems run leases of their
# own - the hook envelope processing lease in hooks/envelope_dedupe.py is one -
# and a bare scan for LEASE_FIELDS cannot tell those from a terminal replica.
GRANT_SURFACE = ("attachment_id", "take_control", "lease_generation")


def _replica_fields(text: str) -> list[str]:
    """Lease fields in one file, or nothing when it is not a terminal grant."""
    if not any(marker in text for marker in GRANT_SURFACE):
        return []
    return [needle for needle in LEASE_FIELDS if needle in text]


def _lease_replicas() -> list[str]:
    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".rs"}:
            continue
        if "__pycache__" in path.parts or "node_modules" in path.parts:
            continue
        if path.name == "test_lease_authority.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits.extend(f"{path.relative_to(ROOT)}:{field}" for field in _replica_fields(text))
    return hits


def test_the_scoped_guard_keeps_its_teeth() -> None:
    """Scoping must not hollow out the guard, and must spare unrelated leases."""
    assert _replica_fields("attachment_id = 'a'\nlease_token = 'stolen-grant'\n") == ["lease_token"]
    assert _replica_fields("take_control()\nlease_ttl = 30\n") == ["lease_ttl"]

    # The hook envelope processing lease: its own lease, no terminal grant.
    envelope_dedupe = (ROOT / "src/gobby/hooks/envelope_dedupe.py").read_text(encoding="utf-8")
    assert "lease_expiry" in envelope_dedupe
    assert _replica_fields(envelope_dedupe) == []

"""Wake admission is decided by pane focus, never by what the CLI drew (#25901)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Literal
from unittest.mock import MagicMock

import pytest

from gobby.events.live_wake import PANE_FOCUSED, RETRYABLE_WAKE_SKIPS
from gobby.events.wake import WakeDispatcher
from gobby.mcp_proxy.tools.sessions import _terminal_send_keys_gate as gate
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.terminals import foreground
from gobby.terminals.composer_ledger import ComposerLedger
from gobby.terminals.leases import TerminalLeaseRegistry
from tests.events.test_wake_composer_attention import HOST_EPOCH, TERMINAL_ID, Seat, _seat
from tests.events.wake_test_support import plain_message
from tests.terminal_scene import SHELL_PID

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# What a seat's pane showed while its wakes were declined: finished prose naming tests that
# run elsewhere, a diff naming a trust folder, and a spinner row without its timer.
BUSY_LOOKING_PANE = (
    "⏺ Still in flight:\n"
    "  - #25855, #25850, #25790: still resolving conflicts and running tests. 1 to 2 h.\n"
    '      12   e2e-trust ANSWERED "I\'m in bed, use a ~/.gobby/ folder"\n'
    "✻ Running…\n"
    "────────────────────\n❯ \n────────────────────"
)


@pytest.fixture(autouse=True)
def _park_composer_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff retries park, so only a focus release re-dispatches a held wake."""

    async def park(_self: WakeDispatcher, _delay: float) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(WakeDispatcher, "_composer_retry_wait", park)


@pytest.fixture
def seat(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    composer_ledger: ComposerLedger,
) -> Iterator[Seat]:
    built = _seat(temp_db, session_manager, sample_project)
    built.session.status = "paused"
    messages = MagicMock()
    messages.get_undelivered_wake_messages.return_value = [plain_message(built.session.id)]
    messages.get_undelivered_messages.return_value = [plain_message(built.session.id)]
    built.dispatcher._ism_manager = messages
    composer_ledger.record_spawn(TERMINAL_ID, HOST_EPOCH)
    yield built
    for task in [
        *built.dispatcher._deferred_refreshes.values(),
        *built.dispatcher._composer_retries.values(),
        *built.dispatcher._focus_releases,
    ]:
        task.cancel()


@pytest.fixture
def no_pane_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record any pane read a wake makes; a wake must make none.

    The activity probe stays: it reads the keystroke ledger and the transcript file.
    """
    reads: list[str] = []

    def pane(*_args: Any) -> Any:
        reads.append("pane_snapshot")
        raise AssertionError("a wake read the pane")

    monkeypatch.setattr(gate, "live_runtime_pane", pane)
    return reads


async def _viewer(
    dispatcher: WakeDispatcher,
    *,
    viewer: Literal["web", "gclient"] = "gclient",
    focused: bool,
) -> tuple[TerminalLeaseRegistry, str]:
    """A viewer holding the seat's terminal, reporting focus as its client would."""
    registry = TerminalLeaseRegistry()
    dispatcher.set_lease_registry(registry)
    registry.set_focus_observer(dispatcher.release_focus_hold)
    record = await registry.attach(TERMINAL_ID, viewer=viewer)
    await registry.take_control(TERMINAL_ID, record.attachment_id)
    if focused:
        await registry.set_focus(record.attachment_id, True)
    return registry, record.attachment_id


@pytest.mark.parametrize("viewer", ["gclient", "web"])
async def test_an_unfocused_pane_gets_the_wake(
    seat: Seat, no_pane_reads: list[str], viewer: Literal["web", "gclient"]
) -> None:
    await _viewer(seat.dispatcher, viewer=viewer, focused=False)

    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

    assert result["delivered"] is True
    seat.pane_sender.assert_awaited_once()
    assert seat.dispatcher._focus_holds == {}
    assert no_pane_reads == []


async def test_without_a_lease_registry_the_wake_is_sent(
    seat: Seat, no_pane_reads: list[str]
) -> None:
    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

    assert result["delivered"] is True
    seat.pane_sender.assert_awaited_once()
    assert no_pane_reads == []


@pytest.mark.parametrize("viewer", ["gclient", "web"])
async def test_a_focused_pane_holds_every_wake(
    seat: Seat, no_pane_reads: list[str], viewer: Literal["web", "gclient"]
) -> None:
    """An agent-to-agent wake holds too, not only operator comms (plan decision 14 superseded)."""
    await _viewer(seat.dispatcher, viewer=viewer, focused=True)

    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

    assert result["delivered"] is False
    assert result["skipped"] == PANE_FOCUSED
    assert PANE_FOCUSED in RETRYABLE_WAKE_SKIPS
    seat.pane_sender.assert_not_awaited()
    assert seat.dispatcher._focus_holds == {TERMINAL_ID: {seat.session.id}}
    assert no_pane_reads == []


async def test_focus_lost_after_the_idle_window_sends_the_held_wake(
    seat: Seat, no_pane_reads: list[str]
) -> None:
    """The client reports focus lost once typing has been idle for its window; that sends."""
    registry, attachment_id = await _viewer(seat.dispatcher, focused=True)
    held = await seat.dispatcher.dispatch_live_wake(seat.session.id)
    assert held["skipped"] == PANE_FOCUSED

    await registry.set_focus(attachment_id, False)
    results = await asyncio.gather(*list(seat.dispatcher._focus_releases))

    assert [result["delivered"] for result in results] == [True]
    seat.pane_sender.assert_awaited_once()
    assert seat.dispatcher._focus_holds == {}
    assert no_pane_reads == []


async def test_busy_looking_pane_text_does_not_block_an_unfocused_wake(
    seat: Seat,
    no_pane_reads: list[str],
    agent_target: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_target(foreground="claude", snapshot=BUSY_LOOKING_PANE)
    # The scene installs a scripted pane; a wake must still never read it.
    monkeypatch.setattr(gate, "live_runtime_pane", lambda *_args: no_pane_reads.append("pane"))
    await _viewer(seat.dispatcher, focused=False)

    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

    assert result["delivered"] is True
    seat.pane_sender.assert_awaited_once()
    assert no_pane_reads == []


async def test_a_typed_draft_still_withholds_an_unfocused_wake(
    seat: Seat, composer_ledger: ComposerLedger
) -> None:
    composer_ledger.observe_write(TERMINAL_ID, origin="operator", kind="text", payload="half a")
    await _viewer(seat.dispatcher, focused=False)

    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

    assert result["skipped"] == "composer_occupied"
    seat.pane_sender.assert_not_awaited()


async def test_a_cursor_only_draft_withholds_an_unfocused_wake(
    seat: Seat, composer_ledger: ComposerLedger, no_pane_reads: list[str]
) -> None:
    # Up may recall history longer than the bounded drain clears; no pane read proves it gone.
    composer_ledger.observe_write(TERMINAL_ID, origin="operator", kind="input", payload="\x1b[A")
    await _viewer(seat.dispatcher, focused=False)

    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

    assert result["skipped"] == "composer_occupied"
    seat.pane_sender.assert_not_awaited()
    assert no_pane_reads == []
    item = seat.attention.get(seat.entry_id)
    assert item is not None
    assert item.state == "blocked"
    assert item.fingerprint == "composer_blocked:cursor"


async def test_a_bare_shell_foreground_is_never_typed_into(
    seat: Seat, agent_target: Callable[..., None]
) -> None:
    agent_target(foreground="zsh", snapshot="")

    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

    assert result["delivered"] is False
    assert result["skipped"] == "send_keys_target_bare_shell"
    seat.pane_sender.assert_not_awaited()


LEADER_PID = 8642
PROVIDER_PID = 8643

# Each supported CLI as the kernel reports it: the 16-character accounting name ``ps`` shows,
# and the on-disk build (relative to a temp home) the process actually executes.
CLI_BUILDS: dict[str, tuple[str, str]] = {
    "claude": ("2.1.322", ".local/share/claude/versions/2.1.322"),
    "codex": ("codex", "Caskroom/codex/0.166.0/bin/codex"),
    "droid": ("droid", ".local/bin/droid"),
    "grok": ("grok-1.0.77-maco", ".grok/downloads/grok-1.0.77-macos-aarch64"),
    "agy": ("agy", ".local/bin/agy"),
}


@pytest.mark.parametrize("cli", sorted(CLI_BUILDS))
@pytest.mark.parametrize("launch", ["plain", "srt"])
async def test_every_supported_cli_gets_the_wake(
    seat: Seat,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_pane_reads: list[str],
    cli: str,
    launch: Literal["plain", "srt"],
) -> None:
    """Claude, Codex, Droid, Grok and AGY take a wake, run plain or under the SRT runner.

    The real ``foreground_executables`` runs over a scripted process table, so a versioned
    build whose accounting name is a truncated version string is still identified.
    """
    comm, relative = CLI_BUILDS[cli]
    build = tmp_path / relative
    build.parent.mkdir(parents=True)
    build.write_bytes(b"")
    monkeypatch.setattr(gate, "_installed_cli", lambda name: build if name == cli else None)
    node = tmp_path / "node"
    leader_comm, leader_exe = (comm, build) if launch == "plain" else ("node", node)
    monkeypatch.setattr(gate, "foreground_executables", foreground.foreground_executables)
    monkeypatch.setattr(
        foreground,
        "_process_snapshot",
        lambda: {SHELL_PID: (LEADER_PID, "zsh"), LEADER_PID: (LEADER_PID, leader_comm)},
    )
    monkeypatch.setattr(
        foreground, "_executable_path", lambda pid: str(leader_exe) if pid == LEADER_PID else None
    )
    runner = tmp_path / "runner.mjs"
    monkeypatch.setattr(gate, "foreground_node_script", lambda _pid: str(runner))
    monkeypatch.setattr(gate, "_is_installed_srt_runner", lambda script: script == str(runner))
    # Under SRT the seat records the provider the runner launched, which ran its hooks.
    context = seat.session.terminal_context
    assert isinstance(context, dict)
    context.update(parent_pid=PROVIDER_PID, parent_create_time=1.0, parent_name=comm)
    monkeypatch.setattr(
        gate,
        "process_identity_matches",
        lambda pid, context: pid == PROVIDER_PID == context.get("parent_pid"),
    )
    monkeypatch.setattr(
        gate,
        "group_and_executable",
        lambda pid: (LEADER_PID, str(build)) if pid == PROVIDER_PID else None,
    )

    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

    assert result["delivered"] is True, result
    seat.pane_sender.assert_awaited_once()
    assert no_pane_reads == []


async def test_a_truncated_version_name_without_its_build_is_refused(
    seat: Seat, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The accounting name alone never proves a versioned build: no path, no wake."""
    monkeypatch.setattr(gate, "foreground_executables", foreground.foreground_executables)
    monkeypatch.setattr(
        foreground,
        "_process_snapshot",
        lambda: {SHELL_PID: (LEADER_PID, "zsh"), LEADER_PID: (LEADER_PID, "2.1.322")},
    )
    monkeypatch.setattr(foreground, "_executable_path", lambda _pid: None)

    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

    assert result["delivered"] is False
    assert result["skipped"] == "send_keys_target_unconfirmed"
    seat.pane_sender.assert_not_awaited()


async def test_an_active_row_holds_regardless_of_focus(seat: Seat) -> None:
    seat.session.status = "active"
    await _viewer(seat.dispatcher, focused=False)

    result = await seat.dispatcher.dispatch_live_wake(seat.session.id)

    assert result["skipped"] == "session_active"
    seat.pane_sender.assert_not_awaited()

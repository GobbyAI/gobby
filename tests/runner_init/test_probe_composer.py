"""The wake composer probe reads managed terminal snapshots through their runtime."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from gobby.agents.idle_detector import ComposerRead
from gobby.events.live_wake import TerminalActivity
from gobby.runner_init.wake_activity import probe_terminal_activity
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.services import TerminalServices
from gobby.terminals.write_coordinator import WriteCoordinator
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

pytestmark = pytest.mark.unit

_RULE = "─" * 20
# Claude Code's styled prompt suggestion after a finished turn.
_SUGGESTION_PANE = (
    f"⏺ done\n{_RULE}\n\x1b[39m❯\xa0\x1b[2mrun\x1b[0m \x1b[2mthe tests\x1b[0m\n{_RULE}\n"
    "   Fable 5.1  12%\n"
)


@pytest.mark.asyncio
async def test_managed_terminal_suggestion_reads_empty_from_an_ansi_snapshot() -> None:
    terminal = make_memory_terminal(terminal_id="term-1", session_name="term-1", backend="native")
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime(backend="native", snapshot_text=_SUGGESTION_PANE)
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    services = TerminalServices(
        manager=store,
        registry=registry,
        coordinator=WriteCoordinator(
            store,
            runtime_registry(runtime),
            lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
        ),
    )
    runner = SimpleNamespace(
        detection_registry=BundledDetectionRegistry(), terminal_services=services
    )
    session = SimpleNamespace(id="session-1", source="claude")

    read = await probe_terminal_activity(cast("GobbyRunner", runner), session, terminal)

    assert read == TerminalActivity(ComposerRead("empty"))
    assert runtime.snapshot_modes == ["ansi"]


@pytest.mark.asyncio
async def test_raw_tmux_context_has_no_activity_probe() -> None:
    runner = SimpleNamespace(detection_registry=BundledDetectionRegistry(), terminal_services=None)
    session = SimpleNamespace(
        id="session-1", source="claude", terminal_context={"tmux_pane": "%12"}
    )

    read = await probe_terminal_activity(cast("GobbyRunner", runner), session, None)

    assert read == TerminalActivity(ComposerRead("unknown"))


_DRAFT_PANE = f"⏺ done\n{_RULE}\n\x1b[39m❯\xa0half-typed wor\x1b[0m\n{_RULE}\n   Fable 5.1  12%\n"


def _runner_for(runtime: FakeRuntime, terminal: Any) -> SimpleNamespace:
    store = MemoryTerminalStore(terminal)
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    services = TerminalServices(
        manager=store,
        registry=registry,
        coordinator=WriteCoordinator(
            store,
            runtime_registry(runtime),
            lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
        ),
    )
    return SimpleNamespace(
        detection_registry=BundledDetectionRegistry(), terminal_services=services
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("pane", "state"), [(_SUGGESTION_PANE, "empty"), (_DRAFT_PANE, "draft")])
async def test_registered_tmux_terminal_is_probed_through_its_runtime(
    pane: str, state: str
) -> None:
    """A managed tmux row gets the same real probe as a native one (#22915).

    The wake gate withholds every ``unknown`` read, so a registered backend the
    probe skipped would never receive a live wake.
    """
    terminal = make_memory_terminal(terminal_id="term-t", session_name="term-t", backend="tmux")
    runtime = FakeRuntime(backend="tmux", snapshot_text=pane)
    runner = _runner_for(runtime, terminal)
    session = SimpleNamespace(id="session-1", source="claude")

    read = await probe_terminal_activity(cast("GobbyRunner", runner), session, terminal)

    assert read.composer.state == state
    assert runtime.snapshot_modes == ["ansi"]


@pytest.mark.asyncio
async def test_unregistered_backend_reads_unknown_and_stays_withheld() -> None:
    """A row whose runtime is not registered fails closed as unknown."""
    terminal = make_memory_terminal(terminal_id="term-t", session_name="term-t", backend="tmux")
    native = FakeRuntime(backend="native", snapshot_text=_SUGGESTION_PANE)
    runner = _runner_for(native, terminal)
    session = SimpleNamespace(id="session-1", source="claude")

    read = await probe_terminal_activity(cast("GobbyRunner", runner), session, terminal)

    assert read == TerminalActivity(ComposerRead("unknown"))
    assert native.snapshot_modes == []

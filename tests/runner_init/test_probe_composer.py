"""The wake dispatcher's composer probe reads a styled snapshot on both terminal paths."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from gobby.agents.idle_detector import ComposerRead
from gobby.runner_init.orchestration import _probe_composer
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.runtime import SnapshotMode
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
# Claude Code's prompt suggestion after a finished turn, as tmux ``capture-pane -e`` renders it.
_SUGGESTION_PANE = (
    f"⏺ done\n{_RULE}\n\x1b[39m❯\xa0\x1b[2mrun\x1b[0m \x1b[2mthe tests\x1b[0m\n{_RULE}\n"
    "   Fable 5.1  12%\n"
)


class _Tmux:
    def __init__(self) -> None:
        self.modes: list[SnapshotMode] = []

    async def snapshot_lines(
        self, target: str, *, lines: int, mode: SnapshotMode = "text"
    ) -> str | None:
        self.modes.append(mode)
        return _SUGGESTION_PANE


@pytest.mark.asyncio
async def test_managed_terminal_suggestion_reads_empty_from_an_ansi_snapshot() -> None:
    terminal = make_memory_terminal(terminal_id="term-1", session_name="term-1")
    store = MemoryTerminalStore(terminal)
    runtime = FakeRuntime(snapshot_text=_SUGGESTION_PANE)
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

    read = await _probe_composer(cast("GobbyRunner", runner), session, terminal)

    assert read == ComposerRead("empty")
    assert runtime.snapshot_modes == ["ansi"]


@pytest.mark.asyncio
async def test_raw_tmux_pane_suggestion_reads_empty_from_an_ansi_snapshot() -> None:
    tmux = _Tmux()
    runner = SimpleNamespace(detection_registry=BundledDetectionRegistry(), terminal_services=None)
    session = SimpleNamespace(
        id="session-1", source="claude", terminal_context={"tmux_pane": "%12"}
    )

    with patch("gobby.terminals.lookup.manager_for_terminal_context", return_value=tmux):
        read = await _probe_composer(cast("GobbyRunner", runner), session, None)

    assert read == ComposerRead("empty")
    assert tmux.modes == ["ansi"]

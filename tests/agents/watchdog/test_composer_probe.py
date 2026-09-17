"""The watchdog's composer probe reads a styled snapshot."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.agents.idle_detector import IdleDetector
from gobby.agents.watchdog.composer_probe import composer_holds_draft
from gobby.storage.agents import AgentRun
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

pytestmark = pytest.mark.unit

_RULE = "─" * 20


def _services(runtime: FakeRuntime) -> TerminalServices:
    store = MemoryTerminalStore(
        make_memory_terminal(terminal_id="agent-run-1", session_name="agent-run-1")
    )
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    return TerminalServices(
        manager=store,
        registry=registry,
        coordinator=WriteCoordinator(
            store,
            runtime_registry(runtime),
            lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "holds_draft"),
    [
        ("\x1b[39m❯\xa0\x1b[2mrun\x1b[0m \x1b[2mthe tests\x1b[0m", False),
        ("\x1b[39m❯\xa0operator draft", True),
    ],
    ids=["faint-suggestion", "typed-draft"],
)
async def test_composer_probe_reads_faint_suggestions_as_empty(row: str, holds_draft: bool) -> None:
    runtime = FakeRuntime()
    runtime.snapshot_text = f"⏺ done\n{_RULE}\n{row}\n{_RULE}\n   Fable 5.1  12%\n"
    run = AgentRun(
        id="run-1",
        parent_session_id="parent-1",
        provider="claude",
        prompt="test",
        status="running",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        terminal_id="agent-run-1",
    )

    held = await composer_holds_draft(
        _services(runtime), IdleDetector(BundledDetectionRegistry()), run
    )

    assert held is holds_draft
    assert runtime.snapshot_modes == ["ansi"]

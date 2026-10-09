"""The watchdog writes only into a composer the ledger reads clean."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from gobby.agents.watchdog.composer_gate import composer_refuses_automation
from gobby.storage.agents import AgentRun
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.composer_ledger import ComposerLedger
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.services import TerminalServices
from gobby.terminals.write_coordinator import WriteCoordinator
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit

_TERMINAL = "agent-run-1"


def _services(store: MemoryTerminalStore) -> TerminalServices:
    runtime = FakeRuntime()
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


def _run(terminal_id: str = _TERMINAL) -> AgentRun:
    return AgentRun(
        id="run-1",
        parent_session_id="parent-1",
        provider="claude",
        prompt="test",
        status="running",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        terminal_id=terminal_id,
    )


def _clean(ledger: ComposerLedger) -> None:
    ledger.record_spawn(_TERMINAL, "")


def _held(ledger: ComposerLedger) -> None:
    _clean(ledger)
    ledger.observe_write(_TERMINAL, origin="daemon", kind="text", payload="continue")


def _draft(ledger: ComposerLedger) -> None:
    _clean(ledger)
    ledger.observe_write(_TERMINAL, origin="operator", kind="text", payload="operator draft")


def _usage_limit(ledger: ComposerLedger) -> None:
    _clean(ledger)
    ledger.block(_TERMINAL, "provider_limit")


def _untracked(_ledger: ComposerLedger) -> None:
    return None


@pytest.mark.parametrize(
    ("seed", "refuses"),
    [
        (_clean, False),
        (_held, False),
        (_draft, True),
        (_usage_limit, True),
        (_untracked, True),
    ],
    ids=["clean", "held", "draft", "usage-limit", "untracked"],
)
def test_watchdog_writes_only_into_a_clean_composer(
    composer_ledger: ComposerLedger, seed: Callable[[ComposerLedger], None], refuses: bool
) -> None:
    seed(composer_ledger)
    store = MemoryTerminalStore(make_memory_terminal(terminal_id=_TERMINAL))

    assert composer_refuses_automation(_services(store), _run()) is refuses


def test_a_run_without_a_live_terminal_is_left_to_the_write(
    composer_ledger: ComposerLedger,
) -> None:
    store = MemoryTerminalStore(make_memory_terminal(terminal_id=_TERMINAL))

    assert composer_refuses_automation(_services(store), _run("gone")) is False

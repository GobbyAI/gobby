"""Fallback terminal-service composition for the agent lifecycle monitor."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import TerminalManager
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.services import TerminalServices
from gobby.terminals.write_coordinator import WriteCoordinator


def build_terminal_services(
    db: HubDatabase,
    lease_registry: TerminalLeaseRegistry,
) -> TerminalServices:
    """Build standalone monitor services without a live terminal backend."""
    manager = TerminalManager(db)
    runtime_registry = TerminalRuntimeRegistry()
    return TerminalServices(
        manager=manager,
        registry=runtime_registry,
        coordinator=WriteCoordinator(
            manager,
            runtime_registry,
            lease_registry=lease_registry,
        ),
    )

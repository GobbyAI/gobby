"""Fallback terminal-service composition for the agent lifecycle monitor."""

from __future__ import annotations

from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import TerminalManager
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.services import TerminalServices
from gobby.terminals.tmux_runtime import TmuxTerminalRuntime
from gobby.terminals.write_coordinator import WriteCoordinator


def build_terminal_services(
    db: HubDatabase,
    tmux: TmuxSessionManager,
    lease_registry: TerminalLeaseRegistry,
) -> TerminalServices:
    """Build the monitor's standalone tmux services around one lease registry."""
    manager = TerminalManager(db)
    runtime_registry = TerminalRuntimeRegistry()
    runtime_registry.register(TmuxTerminalRuntime(tmux))
    return TerminalServices(
        manager=manager,
        registry=runtime_registry,
        coordinator=WriteCoordinator(
            manager,
            runtime_registry,
            lease_registry=lease_registry,
        ),
    )

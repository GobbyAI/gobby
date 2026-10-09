"""Composition root for daemon-owned terminal runtime services."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.runner import GobbyRunner


class _WakeWriteServices:
    manager: Any = None
    coordinator: Any = None


_WAKE_WRITE_SERVICES = _WakeWriteServices()


def wake_write_services() -> tuple[Any, Any]:
    if _WAKE_WRITE_SERVICES.manager is None or _WAKE_WRITE_SERVICES.coordinator is None:
        raise RuntimeError("wake write services are not bound")
    return _WAKE_WRITE_SERVICES.manager, _WAKE_WRITE_SERVICES.coordinator


def bind_wake_write_services(manager: Any, coordinator: Any) -> None:
    """Bind composition-root write services for daemon wake delivery."""
    _WAKE_WRITE_SERVICES.manager = manager
    _WAKE_WRITE_SERVICES.coordinator = coordinator


def init_terminal_wiring(runner: GobbyRunner, config: DaemonConfig) -> None:
    """Build each runner-owned terminal service exactly once."""
    from gobby.config.terminal_host import TerminalHostConfig
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.terminals import TerminalManager
    from gobby.storage.workspaces import WorkspaceManager
    from gobby.terminals import TerminalRuntimeRegistry
    from gobby.terminals.composer_ledger import (
        bind_composer_ledger,
        composer_ledger_path,
        load_ledger,
    )
    from gobby.terminals.composer_lock import bind_composer_coordinator
    from gobby.terminals.host_manager import TerminalHostManager
    from gobby.terminals.input_grants import sync_host_input_grant
    from gobby.terminals.leases import HolderChange, TerminalLeaseRegistry
    from gobby.terminals.native_runtime import HostManagerControl, NativeTerminalRuntime
    from gobby.terminals.services import TerminalServices
    from gobby.terminals.sync_bridge import TerminalEffectBridge
    from gobby.terminals.tmux_runtime import TmuxTerminalRuntime
    from gobby.terminals.write_coordinator import WriteCoordinator
    from gobby.utils.machine_id import require_machine_id

    runner.terminal_manager = TerminalManager(runner.database)
    runner.terminal_config = config.terminals
    host_config = getattr(config, "terminal_host", None) or TerminalHostConfig()
    runner.terminal_host_config = host_config
    runner.workspace_manager = WorkspaceManager(runner.database)
    bootstrap = getattr(runner, "bootstrap_config", None)
    if (
        bootstrap is not None
        and bootstrap.front_door.enabled
        and bootstrap.front_door.routes.get("terminal_ws") == "native"
    ):
        # gdaemon owns Native control, leases and writes. Even a hello from a
        # second supervisor would take the host's control-owner connection.
        runner.terminal_host_manager = None
        runner.lease_registry = None
        runner.frame_client = None
        runner.write_coordinator = None
        runner.terminal_effect_bridge = None
        runner.terminal_services = None
        runner.terminal_runtime_registry = TerminalRuntimeRegistry()
        bind_wake_write_services(None, None)
        bind_composer_coordinator(None)
        runner.wake_dispatcher.set_terminal_manager(runner.terminal_manager)
        if runner.agent_runner is not None:
            runner.agent_runner.terminal_manager = runner.terminal_manager
            runner.agent_runner.terminal_runtime_registry = runner.terminal_runtime_registry
            runner.agent_runner.terminal_config = runner.terminal_config
            runner.agent_runner.write_coordinator = None
            runner.agent_runner.terminal_services = None
        return

    runner.lease_registry = TerminalLeaseRegistry()
    runner.terminal_manager.clear_orphaned_attachment_writes(
        require_machine_id(), runner.lease_registry.daemon_epoch
    )
    runner.terminal_host_manager = TerminalHostManager(
        config=host_config,
        terminal_config=config.terminals,
        terminal_manager=runner.terminal_manager,
        run_manager=LocalAgentRunManager(runner.database),
        tmux_attach_history_lines=config.tmux.attach_history_lines,
    )
    # One composer ledger: host input, daemon writes, and spawns all feed it.
    composer_ledger = load_ledger(composer_ledger_path())
    runner.terminal_host_manager.composer_ledger = composer_ledger
    bind_composer_ledger(composer_ledger)

    terminal_runtime_registry = TerminalRuntimeRegistry()
    native_runtime = NativeTerminalRuntime(
        HostManagerControl(runner.terminal_host_manager),
        terminal_manager=runner.terminal_manager,
        spawn_in_doubt_seconds=config.terminals.spawn_in_doubt_seconds,
        composer_ledger=composer_ledger,
    )
    terminal_runtime_registry.register(native_runtime)
    # Spawn-less adapter for hand-started tmux panes: send_keys, wake, and
    # /compact continuation resolve their external rows through it, each on
    # the socket its pane recorded.
    terminal_runtime_registry.register(
        TmuxTerminalRuntime(host_control=HostManagerControl(runner.terminal_host_manager))
    )

    async def follow_lease_holder(change: HolderChange) -> bool | None:
        return await sync_host_input_grant(
            native_runtime,
            change.terminal,
            change.holder,
            manager=runner.terminal_manager,
            reason=change.reason,
        )

    # The gterm input grant mirrors the writer lease; this is the one place it
    # is wired, so every take, release, and socket loss re-syncs the host.
    runner.lease_registry.set_holder_observer(follow_lease_holder)
    runner.frame_client = getattr(runner.terminal_host_manager, "_frame_client", None)
    runner.terminal_runtime_registry = terminal_runtime_registry
    runner.write_coordinator = WriteCoordinator(
        runner.terminal_manager,
        terminal_runtime_registry,
        lease_registry=runner.lease_registry,
        composer_ledger=composer_ledger,
    )
    bind_wake_write_services(runner.terminal_manager, runner.write_coordinator)
    bind_composer_coordinator(runner.write_coordinator)
    runner.wake_dispatcher.set_terminal_manager(runner.terminal_manager)
    runner.terminal_services = TerminalServices(
        manager=runner.terminal_manager,
        registry=runner.terminal_runtime_registry,
        coordinator=runner.write_coordinator,
    )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    runner.terminal_effect_bridge = (
        None
        if loop is None
        else TerminalEffectBridge(
            loop,
            runner.write_coordinator,
            timeout_seconds=config.terminals.hook_write_timeout_seconds,
            shutdown_timeout_seconds=config.terminals.hook_write_shutdown_timeout_seconds,
        )
    )
    if runner.agent_runner is not None:
        runner.agent_runner.terminal_manager = runner.terminal_manager
        runner.agent_runner.terminal_runtime_registry = runner.terminal_runtime_registry
        runner.agent_runner.terminal_config = runner.terminal_config
        runner.agent_runner.write_coordinator = runner.write_coordinator
        runner.agent_runner.terminal_services = runner.terminal_services

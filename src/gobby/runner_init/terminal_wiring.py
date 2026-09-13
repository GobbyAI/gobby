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
    from gobby.terminals import TerminalRuntimeRegistry
    from gobby.terminals.host_manager import TerminalHostManager
    from gobby.terminals.leases import TerminalLeaseRegistry
    from gobby.terminals.native_runtime import HostManagerControl, NativeTerminalRuntime
    from gobby.terminals.services import TerminalServices
    from gobby.terminals.sync_bridge import TerminalEffectBridge
    from gobby.terminals.tmux_runtime import configured_tmux_runtime
    from gobby.terminals.write_coordinator import WriteCoordinator
    from gobby.utils.machine_id import require_machine_id

    runner.terminal_manager = TerminalManager(runner.database)
    runner.lease_registry = TerminalLeaseRegistry()
    runner.terminal_manager.clear_orphaned_attachment_writes(
        require_machine_id(), runner.lease_registry.daemon_epoch
    )
    runner.terminal_config = config.terminals
    host_config = getattr(config, "terminal_host", None) or TerminalHostConfig()
    runner.terminal_host_config = host_config
    runner.terminal_host_manager = TerminalHostManager(
        config=host_config,
        terminal_config=config.terminals,
        terminal_manager=runner.terminal_manager,
        run_manager=LocalAgentRunManager(runner.database),
        tmux_attach_history_lines=config.tmux.attach_history_lines,
    )

    terminal_runtime_registry = TerminalRuntimeRegistry()
    terminal_runtime_registry.register(configured_tmux_runtime())
    terminal_runtime_registry.register(
        NativeTerminalRuntime(
            HostManagerControl(runner.terminal_host_manager),
            terminal_manager=runner.terminal_manager,
            spawn_in_doubt_seconds=config.terminals.spawn_in_doubt_seconds,
        )
    )
    runner.frame_client = getattr(runner.terminal_host_manager, "_frame_client", None)
    runner.terminal_runtime_registry = terminal_runtime_registry
    runner.write_coordinator = WriteCoordinator(
        runner.terminal_manager,
        terminal_runtime_registry,
        lease_registry=runner.lease_registry,
    )
    bind_wake_write_services(runner.terminal_manager, runner.write_coordinator)
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

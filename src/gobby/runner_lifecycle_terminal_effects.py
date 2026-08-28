"""Drain runner-owned TerminalEffectBridge tasks during graceful shutdown."""

from __future__ import annotations

from typing import Any

from gobby.terminals.sync_bridge import TerminalEffectBridge, clamp_hook_timeout


async def drain_terminal_effects(
    bridge: TerminalEffectBridge | None,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Wait for in-flight hook writes, then quarantine anything still unsettled."""
    if bridge is None:
        return
    await bridge.drain(clamp_hook_timeout(timeout_seconds))


def bridge_from_runner(runner: Any) -> TerminalEffectBridge | None:
    bridge = getattr(runner, "terminal_effect_bridge", None)
    return bridge if isinstance(bridge, TerminalEffectBridge) else None

"""Daemon readiness checks for agent spawn entry points."""

from __future__ import annotations


def spawn_readiness_blocker(services: object | None) -> str | None:
    """Return a reason when daemon lifecycle state should block agent spawning."""
    if services is None:
        return None
    if bool(getattr(services, "shutdown_in_progress", False)):
        return "daemon_shutdown_in_progress"
    if getattr(services, "startup_ready", True) is False:
        return "daemon_startup_not_ready"
    return None

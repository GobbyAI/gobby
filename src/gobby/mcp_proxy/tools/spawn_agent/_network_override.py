"""Validation and authority for a single launch's network override."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gobby.agents.sandbox_network import apply_network_override as apply_network_override
from gobby.workflows.condition_helpers_sessions import network_override_allowed

if TYPE_CHECKING:
    from gobby.storage.sessions import SessionManager


def validate_network_override(network: str | None) -> None:
    if network is not None and network not in ("none", "trusted"):
        raise ValueError("network must be 'none' or 'trusted'")


def enforce_network_override(
    session_manager: SessionManager | None,
    caller_id: str | None,
    network: str | None,
    *,
    internal: bool = False,
) -> None:
    """Called inside the caller guard's worker after identity verification."""
    validate_network_override(network)
    if network is None:
        return
    if internal:
        raise ValueError("network override is unavailable to daemon-internal callers")
    if caller_id is not None and not network_override_allowed(session_manager, caller_id):
        raise ValueError("caller is not authorized to override network")

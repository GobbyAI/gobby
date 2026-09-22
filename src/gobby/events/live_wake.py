"""State-authoritative outcomes for optional live wake dispatch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from gobby.agents.idle_detector import ComposerRead
from gobby.sessions.tmux_context import get_tmux_socket_path, parse_terminal_context_value
from gobby.storage.sessions import LIVE_SESSION_STATUSES, PROTECTED_SESSION_STATUSES


@dataclass(frozen=True)
class TerminalActivity:
    """Composer and provider-turn evidence derived from one terminal snapshot."""

    composer: ComposerRead
    turn_in_flight_fingerprint: str | None = None


# (session, managed terminal row or None) -> one shared terminal activity read.
ActivityProbe = Callable[[Any, Any | None], Awaitable[TerminalActivity]]

COMPOSER_OCCUPIED = "composer_occupied"


def parse_tmux_session(terminal_context: Any) -> str | None:
    ctx = parse_terminal_context_value(terminal_context)
    if not ctx:
        return None
    value = ctx.get("tmux_session")
    return value if isinstance(value, str) and value else None


def parse_tmux_pane(terminal_context: Any) -> str | None:
    ctx = parse_terminal_context_value(terminal_context)
    if not ctx:
        return None
    value = ctx.get("tmux_pane")
    return value if isinstance(value, str) and value else None


def parse_tmux_socket_path(terminal_context: Any) -> str | None:
    ctx = parse_terminal_context_value(terminal_context)
    return get_tmux_socket_path(ctx) if ctx else None


def normalize_live_wake_result(result: dict[str, Any]) -> dict[str, Any]:
    """Attach the stable reason contract for designed non-delivery outcomes."""
    normalized = dict(result)
    if normalized.get("delivered") is not True and "decline_reason" not in normalized:
        skipped = normalized.get("skipped")
        if isinstance(skipped, str) and skipped:
            normalized["decline_reason"] = skipped
    return normalized


def wake_failure(
    session_id: str,
    *,
    method: str | None,
    error_code: str,
    error_message: str,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "delivered": False,
        "method": method,
        "error": error_code,
        "error_code": error_code,
        "error_message": error_message,
    }


def wake_debounced_result(session_id: str, *, method: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "delivered": False,
        "method": method,
        "skipped": "debounced",
        "decline_reason": "debounced",
    }


def wake_state_failure(session_id: str, status: str | None) -> dict[str, Any] | None:
    """Return a no-side-effect wake outcome for protected or non-live sessions."""
    if status in PROTECTED_SESSION_STATUSES:
        error_code = f"session_{status}"
        return {
            "session_id": session_id,
            "session_status": status,
            "delivered": False,
            "method": None,
            "skipped": error_code,
            "error_code": error_code,
        }
    if status not in LIVE_SESSION_STATUSES:
        error_code = "session_expired" if status == "expired" else "session_not_live"
        return {
            "session_id": session_id,
            "session_status": status,
            "delivered": False,
            "method": None,
            "skipped": error_code,
            "error_code": error_code,
        }
    return None


def composer_occupied_result(session_id: str, *, method: str) -> dict[str, Any]:
    """Return the skip outcome for a wake withheld from a composer holding a draft.

    Same bucket as ``session_active``: the durable message is persisted and the
    hook piggyback injects it on the session's next turn, so senders see it as
    sent, never as a failure.
    """
    return {
        "session_id": session_id,
        "delivered": False,
        "method": method,
        "skipped": COMPOSER_OCCUPIED,
        "decline_reason": COMPOSER_OCCUPIED,
        "ism_persisted": True,
    }

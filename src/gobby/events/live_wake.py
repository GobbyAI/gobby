"""State-authoritative outcomes for optional live wake dispatch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from gobby.agents.idle_detector import ComposerRead
from gobby.sessions.handoff import PENDING_HANDOFF_VARIABLE
from gobby.sessions.tmux_context import get_tmux_socket_path, parse_terminal_context_value
from gobby.storage.sessions import LIVE_SESSION_STATUSES, PROTECTED_SESSION_STATUSES
from gobby.workflows.reserved_variables import HANDOFF_TURN_END_PENDING_VARIABLE


@dataclass(frozen=True)
class TerminalActivity:
    """Composer and provider-turn evidence derived from one terminal snapshot."""

    composer: ComposerRead
    turn_in_flight_fingerprint: str | None = None


# (session, managed terminal row or None) -> one shared terminal activity read.
ActivityProbe = Callable[[Any, Any | None], Awaitable[TerminalActivity]]

COMPOSER_OCCUPIED = "composer_occupied"
COMPOSER_UNCONFIRMED = "composer_unconfirmed"

# Skips that must be retried rather than treated as a final outcome: the durable
# message is persisted and the composer may clear on a later probe.
RETRYABLE_WAKE_SKIPS = frozenset({"session_active", COMPOSER_OCCUPIED, COMPOSER_UNCONFIRMED})


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


def composer_unconfirmed_result(session_id: str, *, method: str) -> dict[str, Any]:
    """Return the skip outcome for a wake withheld from an unreadable composer.

    An ``unknown`` read is not permission to type: the composer may hold an
    operator draft the probe could not classify. Same durable bucket as
    ``composer_occupied_result``, so senders still see the message as sent and
    the wake retries until a probe positively confirms an empty composer.
    """
    return {
        "session_id": session_id,
        "delivered": False,
        "method": method,
        "skipped": COMPOSER_UNCONFIRMED,
        "decline_reason": COMPOSER_UNCONFIRMED,
        "ism_persisted": True,
    }


def handoff_delivery_skip(session_id: str, variables: Mapping[str, Any]) -> dict[str, Any] | None:
    """Keep wake input out of a composer a staged set_handoff dispatch owns."""
    if variables.get(HANDOFF_TURN_END_PENDING_VARIABLE) is not True or not isinstance(
        variables.get(PENDING_HANDOFF_VARIABLE), Mapping
    ):
        return None
    return {
        "session_id": session_id,
        "delivered": False,
        "method": "next_call_context",
        "skipped": "handoff_delivery_pending",
        "ism_persisted": True,
    }

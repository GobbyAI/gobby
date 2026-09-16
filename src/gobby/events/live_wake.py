"""State-authoritative outcomes for optional live wake dispatch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.storage.sessions import LIVE_SESSION_STATUSES, PROTECTED_SESSION_STATUSES

if TYPE_CHECKING:
    from gobby.agents.idle_detector import ComposerRead

# (session, managed terminal row or None) -> what the composer shows.
ComposerProbe = Callable[[Any, Any | None], Awaitable["ComposerRead"]]

COMPOSER_OCCUPIED = "composer_occupied"


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
        "ism_persisted": True,
    }

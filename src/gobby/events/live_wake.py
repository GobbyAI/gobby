"""State-authoritative outcomes for optional live wake dispatch."""

from __future__ import annotations

from typing import Any

from gobby.storage.sessions import LIVE_SESSION_STATUSES, PROTECTED_SESSION_STATUSES


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

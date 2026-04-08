"""Lightweight savings recorder for use in MCP tools and other contexts.

Uses the app context to get the DB, falling back silently if unavailable.
All operations are best-effort — failures are logged but never raised.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def record_savings(
    category: str,
    original_chars: int,
    actual_chars: int,
    session_id: str | None = None,
    project_id: str | None = None,
    model: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a savings event (best-effort, never raises)."""
    try:
        tracker = _get_tracker()
        if tracker is None:
            return
        tracker.record(
            category=category,
            original_chars=original_chars,
            actual_chars=actual_chars,
            session_id=session_id,
            project_id=project_id,
            model=model,
            metadata=metadata,
        )
    except Exception as e:
        logger.debug(f"Failed to record savings: {e}")


def record_savings_tokens(
    category: str,
    original_tokens: int,
    actual_tokens: int,
    session_id: str | None = None,
    project_id: str | None = None,
    model: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a savings event in tokens (best-effort, never raises)."""
    try:
        tracker = _get_tracker()
        if tracker is None:
            return
        tracker.record_tokens(
            category=category,
            original_tokens=original_tokens,
            actual_tokens=actual_tokens,
            session_id=session_id,
            project_id=project_id,
            model=model,
            metadata=metadata,
        )
    except Exception as e:
        logger.debug(f"Failed to record savings: {e}")


def _get_tracker() -> Any:
    """Get SavingsTracker from app context, or None."""
    try:
        from gobby.app_context import get_app_context

        ctx = get_app_context()
        if ctx is None:
            return None

        # Cache on the context object
        tracker = getattr(ctx, "_savings_tracker", None)
        if tracker is not None:
            return tracker

        from gobby.savings.tracker import SavingsTracker

        tracker = SavingsTracker(db=ctx.database)
        ctx._savings_tracker = tracker
        return tracker
    except Exception:
        return None

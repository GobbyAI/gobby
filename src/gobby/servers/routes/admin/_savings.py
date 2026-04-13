"""Savings tracking API endpoints."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def register_savings_routes(router: APIRouter, server: "HTTPServer") -> None:
    @router.get("/savings")
    async def get_savings(
        days: int = 1,
        hours: int | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Get savings summary for the specified time window.

        Args:
            days: Time window in days. Used when hours is not set.
            hours: Time window in hours. Takes precedence over days.
                   0 = all time (maps to a large days value).
            project_id: Filter to a specific project.
        """
        tracker = _get_tracker(server)
        if tracker is None:
            return {"error": "Savings tracker not available", "days": days}
        # Convert hours to days for the tracker
        if hours is not None:
            if hours == 0:
                effective_days = 36500  # all time
            else:
                # Round up to at least 1 day for sub-day granularity
                effective_days = max(1, -(-hours // 24))  # ceiling division
        else:
            effective_days = days
        result: dict[str, Any] = tracker.get_summary(days=effective_days, project_id=project_id)
        return result

    @router.get("/savings/cumulative")
    async def get_cumulative_savings(
        days: int = 30, project_id: str | None = None
    ) -> dict[str, Any]:
        """Get cumulative savings over a longer window."""
        tracker = _get_tracker(server)
        if tracker is None:
            return {"error": "Savings tracker not available", "days": days}
        result: dict[str, Any] = tracker.get_cumulative(days=days, project_id=project_id)
        return result

    @router.post("/savings/record")
    async def record_savings(body: dict[str, Any]) -> dict[str, Any]:
        """Record a savings event from an external caller (e.g. gsqz).

        When ``model`` is not provided, the endpoint resolves it from
        the most recently active session — callers like gsqz only need
        to report chars.
        """
        tracker = _get_tracker(server)
        if tracker is None:
            return {"success": False, "error": "Savings tracker not available"}

        category = body.get("category", "")
        if not category:
            return {"success": False, "error": "category is required"}

        from gobby.savings.tracker import VALID_CATEGORIES

        if category not in VALID_CATEGORIES:
            return {
                "success": False,
                "error": f"Invalid category {category!r}. Valid: {sorted(VALID_CATEGORIES)}",
            }

        # Resolve model from active session when caller doesn't provide one
        model = body.get("model") or await _resolve_active_model(server)

        # Support both chars and tokens
        if "original_tokens" in body:
            tracker.record_tokens(
                category=category,
                original_tokens=body.get("original_tokens", 0),
                actual_tokens=body.get("actual_tokens", 0),
                session_id=body.get("session_id"),
                project_id=body.get("project_id"),
                model=model,
                metadata=body.get("metadata"),
            )
        else:
            tracker.record(
                category=category,
                original_chars=body.get("original_chars", 0),
                actual_chars=body.get("actual_chars", 0),
                session_id=body.get("session_id"),
                project_id=body.get("project_id"),
                model=model,
                metadata=body.get("metadata"),
            )

        return {"success": True}


async def _resolve_active_model(server: "HTTPServer") -> str | None:
    """Look up the model from the most recently updated session."""
    try:
        row = await asyncio.to_thread(
            server.services.database.fetchone,
            "SELECT model FROM sessions WHERE model IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
        )
        return row["model"] if row else None
    except Exception as e:
        logger.debug("Failed to resolve active model: %s", e)
        return None


def _get_tracker(server: "HTTPServer") -> Any:
    """Get SavingsTracker from server, creating lazily if needed."""
    tracker = getattr(server, "_savings_tracker", None)
    if tracker is not None:
        return tracker

    try:
        from gobby.savings.tracker import SavingsTracker

        db = server.services.database
        tracker = SavingsTracker(db=db)
        server._savings_tracker = tracker
        return tracker
    except Exception as e:
        logger.warning(f"Failed to create SavingsTracker: {e}")
        return None

"""Paginated readers for immutable feedback review evidence."""

from __future__ import annotations

import asyncio
from typing import Any

from gobby.feedback.storage import FeedbackReviewStore
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.session_context import get_current_session_id


def create_feedback_registry(db: HubDatabase) -> InternalToolRegistry:
    registry = InternalToolRegistry(name="gobby-feedback", description="Feedback review evidence")
    store = FeedbackReviewStore(db)

    @registry.tool(name="get_review_observations", description="Read a frozen feedback batch page.")
    async def get_review_observations(
        run_id: str, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        return await asyncio.to_thread(store.observations_page, run_id, offset=offset, limit=limit)

    @registry.tool(
        name="get_review_results", description="Read accepted findings and task outcomes."
    )
    async def get_review_results(run_id: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        return await asyncio.to_thread(store.results_page, run_id, offset=offset, limit=limit)

    @registry.tool(
        name="submit_review",
        description="Submit validated findings for task creation and save the Markdown review report.",
    )
    async def submit_review(
        run_id: str, findings: dict[str, Any], summary_md: str
    ) -> dict[str, Any]:
        session_id = get_current_session_id()
        if not session_id:
            return {"success": False, "error": "An assigned reviewer session is required"}
        try:
            report_path = await asyncio.to_thread(
                store.submit_review, run_id, session_id, findings, summary_md
            )
        except (ValueError, RuntimeError) as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "run_id": run_id, "report_path": report_path}

    return registry

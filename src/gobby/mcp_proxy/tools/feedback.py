"""Paginated readers for immutable feedback review evidence."""

from __future__ import annotations

import asyncio
from typing import Any

from gobby.feedback.storage import FeedbackReviewStore
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.hub.protocol import HubDatabase


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

    return registry

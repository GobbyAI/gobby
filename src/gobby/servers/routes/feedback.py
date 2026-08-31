"""HTTP routes for the session-feedback review loop."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

if TYPE_CHECKING:
    from gobby.feedback.service import FeedbackReviewService
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


class FeedbackReviewRequest(BaseModel):
    dry_run: bool = False


def create_feedback_router(server: HTTPServer) -> APIRouter:
    """Create session-feedback review routes."""
    router = APIRouter(prefix="/feedback", tags=["feedback"])

    def _service() -> FeedbackReviewService:
        service: FeedbackReviewService | None = getattr(
            server.services, "feedback_review_service", None
        )
        if service is None:
            raise HTTPException(status_code=503, detail="feedback review service is unavailable")
        return service

    @router.post("/review")
    async def feedback_review(request: FeedbackReviewRequest) -> dict[str, Any]:
        service = _service()
        # The review runs inline: one distill call plus deterministic task
        # filing. The failed run row is already finalized by the service.
        try:
            result = await service.run_review(dry_run=request.dry_run)
        except Exception as exc:
            logger.exception("Feedback review run failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"success": True, **result}

    @router.get("/review/latest")
    async def feedback_review_latest() -> dict[str, Any]:
        run = _service().store.latest_run()
        if run is None:
            raise HTTPException(status_code=404, detail="no feedback review runs recorded")
        return {"success": True, "run": asdict(run)}

    @router.get("/review/{run_id}")
    async def feedback_review_run(run_id: str) -> dict[str, Any]:
        run = _service().store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"feedback review run not found: {run_id}")
        return {"success": True, "run": asdict(run)}

    return router

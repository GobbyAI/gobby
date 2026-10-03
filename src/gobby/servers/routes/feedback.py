"""HTTP routes for the session-feedback review loop."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gobby.feedback.cron import FEEDBACK_REVIEW_CRON_JOB_NAME
from gobby.sessions.handoff import FEEDBACK_DISPOSITIONS, FEEDBACK_FREQUENCIES, FEEDBACK_KINDS

if TYPE_CHECKING:
    from gobby.feedback.service import FeedbackReviewService
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


class FeedbackReviewRequest(BaseModel):
    dry_run: bool = False


def create_feedback_router(server: HTTPServer) -> APIRouter:
    """Create session-feedback review routes."""
    router = APIRouter(prefix="/api/feedback", tags=["feedback"])

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

    @router.get("/entries")
    async def feedback_entries(
        limit: int = 50,
        unreviewed: bool = False,
        kind: str | None = None,
        frequency: str | None = None,
        disposition: str | None = None,
    ) -> dict[str, Any]:
        for name, value, allowed in (
            ("kind", kind, FEEDBACK_KINDS),
            ("frequency", frequency, FEEDBACK_FREQUENCIES),
            ("disposition", disposition, FEEDBACK_DISPOSITIONS),
        ):
            if value is not None and value not in allowed:
                raise HTTPException(
                    status_code=422, detail=f"{name} must be one of {', '.join(allowed)}"
                )
        try:
            entries = _service().store.list_feedback(
                limit=limit,
                unreviewed=unreviewed,
                kind=kind,
                frequency=frequency,
                disposition=disposition,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"success": True, "entries": [asdict(entry) for entry in entries]}

    @router.get("/runs")
    async def feedback_runs(limit: int = 20) -> dict[str, Any]:
        try:
            runs = _service().store.list_runs(limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"success": True, "runs": [asdict(run) for run in runs]}

    @router.get("/status")
    async def feedback_status() -> dict[str, Any]:
        store = _service().store
        latest = store.list_runs(limit=1)
        cron_storage = getattr(server.services, "cron_storage", None)
        job = (
            cron_storage.get_job_by_name(FEEDBACK_REVIEW_CRON_JOB_NAME)
            if cron_storage is not None
            else None
        )
        return {
            "success": True,
            "backlog": store.backlog_count(),
            "latest_run": asdict(latest[0]) if latest else None,
            "schedule": (
                {
                    "enabled": job.enabled,
                    "cron_expr": job.cron_expr,
                    "timezone": job.timezone,
                    "next_run_at": job.next_run_at,
                    "last_status": job.last_status,
                }
                if job is not None
                else None
            ),
        }

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

    @router.get("/review/{run_id}/observations")
    async def feedback_observations(
        run_id: str, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        try:
            return _service().store.observations_page(run_id, offset=offset, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/review/{run_id}/results")
    async def feedback_results(run_id: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        try:
            return _service().store.results_page(run_id, offset=offset, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router

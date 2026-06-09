"""
Cron job routes for Gobby HTTP server.

Provides endpoints for managing cron jobs and viewing run history.
"""

import logging
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.cron import CronJobStorage

logger = logging.getLogger(__name__)


class CreateCronJobRequest(BaseModel):
    """Request body for POST /api/cron/jobs."""

    name: str
    project_id: str = ""
    description: str | None = None
    schedule_type: Literal["cron", "interval", "once"] = "cron"
    cron_expr: str | None = None
    interval_seconds: int | None = None
    run_at: str | None = None
    timezone: str = "UTC"
    action_type: Literal["agent_spawn", "pipeline", "shell"]
    action_config: dict[str, Any] = Field(default_factory=dict)


class UpdateCronJobRequest(BaseModel):
    """Request body for PATCH /api/cron/jobs/{job_id}."""

    name: str | None = None
    description: str | None = None
    schedule_type: str | None = None
    cron_expr: str | None = None
    interval_seconds: int | None = None
    run_at: str | None = None
    timezone: str | None = None
    action_type: str | None = None
    action_config: dict[str, Any] | None = None
    enabled: bool | None = None


def create_cron_router(server: "HTTPServer") -> APIRouter:
    """
    Create cron router with endpoints bound to server instance.

    Args:
        server: HTTPServer instance for accessing state and dependencies

    Returns:
        Configured APIRouter with cron job endpoints
    """
    router = APIRouter(prefix="/api/cron", tags=["cron"])

    def _get_storage() -> "CronJobStorage":
        from gobby.storage.cron import CronJobStorage

        storage = server.services.cron_storage
        if storage is None:
            raise HTTPException(status_code=503, detail="Cron storage not available")
        if not isinstance(storage, CronJobStorage):
            raise HTTPException(status_code=503, detail="Cron storage not available")
        return storage

    @router.get("/jobs")
    async def list_jobs(
        project_id: str | None = Query(None),
        enabled: bool | None = Query(None),
    ) -> dict[str, Any]:
        """List cron jobs with optional filtering."""
        try:
            storage = _get_storage()
            jobs = storage.list_jobs(project_id=project_id, enabled=enabled)
            return {
                "status": "success",
                "jobs": [j.to_dict() for j in jobs],
                "count": len(jobs),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error listing cron jobs: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/jobs")
    async def create_job(request: CreateCronJobRequest) -> dict[str, Any]:
        """Create a new cron job."""
        try:
            storage = _get_storage()
            job = storage.create_job(
                project_id=request.project_id,
                name=request.name,
                schedule_type=request.schedule_type,
                action_type=request.action_type,
                action_config=request.action_config,
                cron_expr=request.cron_expr,
                interval_seconds=request.interval_seconds,
                run_at=request.run_at,
                timezone=request.timezone,
                description=request.description,
            )
            return {"status": "success", "job": job.to_dict()}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error creating cron job: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        """Get a cron job by ID."""
        try:
            storage = _get_storage()
            job = storage.get_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail=f"Cron job not found: {job_id}")
            return {"status": "success", "job": job.to_dict()}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting cron job: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.patch("/jobs/{job_id}")
    async def update_job(job_id: str, request: UpdateCronJobRequest) -> dict[str, Any]:
        """Update a cron job."""
        try:
            storage = _get_storage()
            kwargs: dict[str, Any] = {}
            for field in [
                "name",
                "description",
                "schedule_type",
                "cron_expr",
                "interval_seconds",
                "run_at",
                "timezone",
                "action_type",
                "action_config",
                "enabled",
            ]:
                val = getattr(request, field)
                if val is not None:
                    kwargs[field] = val

            if not kwargs:
                raise HTTPException(status_code=400, detail="No fields to update")

            updated = storage.update_job(job_id, **kwargs)
            if not updated:
                raise HTTPException(status_code=404, detail=f"Cron job not found: {job_id}")
            return {"status": "success", "job": updated.to_dict()}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating cron job: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.delete("/jobs/{job_id}")
    async def delete_job(job_id: str) -> dict[str, Any]:
        """Delete a cron job."""
        try:
            storage = _get_storage()
            success = storage.delete_job(job_id)
            if not success:
                raise HTTPException(status_code=404, detail=f"Cron job not found: {job_id}")
            return {"status": "success"}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting cron job: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/jobs/{job_id}/toggle")
    async def toggle_job(job_id: str) -> dict[str, Any]:
        """Toggle a cron job enabled/disabled."""
        try:
            storage = _get_storage()
            job = storage.toggle_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail=f"Cron job not found: {job_id}")
            return {"status": "success", "job": job.to_dict()}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error toggling cron job: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.post("/jobs/{job_id}/run", response_model=None)
    async def run_job_now(job_id: str) -> dict[str, Any] | JSONResponse:
        """Trigger immediate execution of a cron job."""
        try:
            scheduler = server.services.cron_scheduler
            if scheduler is not None:
                run = await scheduler.run_now(job_id)
                if not run:
                    raise HTTPException(status_code=404, detail=f"Cron job not found: {job_id}")
                return {"status": "success", "run": run.to_dict()}

            # Scheduler not available - record the run but can't execute
            storage = _get_storage()
            job = storage.get_job(job_id)
            if not job:
                raise HTTPException(status_code=404, detail=f"Cron job not found: {job_id}")
            run = storage.create_run(job.id)
            return JSONResponse(
                status_code=202,
                content={"status": "accepted", "executed": False, "run": run.to_dict()},
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error running cron job: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/jobs/{job_id}/runs")
    async def list_runs(
        job_id: str,
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        """List run history for a cron job."""
        try:
            storage = _get_storage()
            runs = storage.list_runs(job_id, limit=limit)
            return {
                "status": "success",
                "runs": [r.to_dict() for r in runs],
                "count": len(runs),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error listing cron runs: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        """Get a specific cron run by ID."""
        try:
            storage = _get_storage()
            run = storage.get_run(run_id)
            if not run:
                raise HTTPException(status_code=404, detail=f"Cron run not found: {run_id}")
            return {"status": "success", "run": run.to_dict()}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting cron run: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

    return router

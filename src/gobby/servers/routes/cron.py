"""
Cron job routes for Gobby HTTP server.

Provides endpoints for managing cron jobs and viewing run history.
"""

import logging
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from gobby.scheduler.scheduler import CronRunRejected
from gobby.storage.cron import SystemRowProtected, is_removed_automation_job
from gobby.storage.projects import LocalProjectManager

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer
    from gobby.storage.cron import CronJobStorage

logger = logging.getLogger(__name__)


class CreateCronJobRequest(BaseModel):
    """Request body for POST /api/cron/jobs."""

    name: str
    project_id: str | None = None
    description: str | None = None
    schedule_type: Literal["cron", "interval", "once"] = "cron"
    cron_expr: str | None = None
    interval_seconds: int | None = None
    run_at: str | None = None
    timezone: str = "UTC"
    action_type: Literal["agent_spawn", "pipeline", "shell"]
    action_config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_schedule_fields(self) -> "CreateCronJobRequest":
        _validate_schedule_fields(
            self.schedule_type,
            cron_expr=self.cron_expr,
            interval_seconds=self.interval_seconds,
            run_at=self.run_at,
        )
        return self


class UpdateCronJobRequest(BaseModel):
    """Request body for PATCH /api/cron/jobs/{job_id}."""

    name: str | None = None
    description: str | None = None
    schedule_type: Literal["cron", "interval", "once"] | None = None
    cron_expr: str | None = None
    interval_seconds: int | None = None
    run_at: str | None = None
    timezone: str | None = None
    action_type: Literal["agent_spawn", "pipeline", "shell"] | None = None
    action_config: dict[str, Any] | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def validate_schedule_fields(self) -> "UpdateCronJobRequest":
        if self.schedule_type is not None:
            _validate_schedule_fields(
                self.schedule_type,
                cron_expr=self.cron_expr,
                interval_seconds=self.interval_seconds,
                run_at=self.run_at,
            )
        return self


def _validate_schedule_fields(
    schedule_type: Literal["cron", "interval", "once"],
    *,
    cron_expr: str | None,
    interval_seconds: int | None,
    run_at: str | None,
) -> None:
    required_fields = {
        "cron": ("cron_expr", cron_expr),
        "interval": ("interval_seconds", interval_seconds),
        "once": ("run_at", run_at),
    }
    field_name, value = required_fields[schedule_type]
    if value is None or isinstance(value, str) and not value.strip():
        raise ValueError(f"{field_name} is required when schedule_type={schedule_type!r}")


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

    async def _resolve_project_id(project_id: str | None) -> str:
        """Resolve and validate the project used for a new cron job."""
        try:
            resolved_project_id = server.resolve_project_id(project_id, cwd=None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        project_manager = LocalProjectManager(server.services.database)
        project = await server.run_db(project_manager.get, resolved_project_id)
        if project is None:
            raise HTTPException(
                status_code=404,
                detail=f"Project not found: {resolved_project_id}",
            )
        return resolved_project_id

    @router.get("/jobs")
    async def list_jobs(
        project_id: str | None = Query(None),
        enabled: bool | None = Query(None),
    ) -> dict[str, Any]:
        """List cron jobs with optional filtering."""
        try:
            storage = _get_storage()
            jobs = [
                job
                for job in await server.run_db(
                    storage.list_jobs, project_id=project_id, enabled=enabled
                )
                if not is_removed_automation_job(job)
            ]
            return {
                "status": "success",
                "jobs": [j.to_dict() for j in jobs],
                "count": len(jobs),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error listing cron jobs: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/jobs")
    async def create_job(request: CreateCronJobRequest) -> dict[str, Any]:
        """Create a new cron job."""
        try:
            storage = _get_storage()
            project_id = await _resolve_project_id(request.project_id)
            job = await server.run_db(
                storage.create_job,
                project_id=project_id,
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
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Error creating cron job: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        """Get a cron job by ID."""
        try:
            storage = _get_storage()
            job = await server.run_db(storage.get_job, job_id)
            if not job:
                raise HTTPException(status_code=404, detail=f"Cron job not found: {job_id}")
            return {"status": "success", "job": job.to_dict()}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting cron job: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

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

            updated = await server.run_db(storage.update_job, job_id, **kwargs)
            if not updated:
                raise HTTPException(status_code=404, detail=f"Cron job not found: {job_id}")
            return {"status": "success", "job": updated.to_dict()}
        except HTTPException:
            raise
        except SystemRowProtected as e:
            raise HTTPException(status_code=403, detail="System cron job is protected") from e
        except Exception as e:
            logger.error(f"Error updating cron job: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.delete("/jobs/{job_id}")
    async def delete_job(job_id: str) -> dict[str, Any]:
        """Delete a cron job."""
        try:
            storage = _get_storage()
            success = await server.run_db(storage.delete_job, job_id)
            if not success:
                raise HTTPException(status_code=404, detail=f"Cron job not found: {job_id}")
            return {"status": "success"}
        except HTTPException:
            raise
        except SystemRowProtected as e:
            raise HTTPException(status_code=403, detail="System cron job is protected") from e
        except Exception as e:
            logger.error(f"Error deleting cron job: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/jobs/{job_id}/toggle")
    async def toggle_job(job_id: str) -> dict[str, Any]:
        """Toggle a cron job enabled/disabled."""
        try:
            storage = _get_storage()
            job = await server.run_db(storage.toggle_job, job_id)
            if not job:
                raise HTTPException(status_code=404, detail=f"Cron job not found: {job_id}")
            return {"status": "success", "job": job.to_dict()}
        except HTTPException:
            raise
        except SystemRowProtected as e:
            raise HTTPException(status_code=403, detail="System cron job is protected") from e
        except Exception as e:
            logger.error(f"Error toggling cron job: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.post("/jobs/{job_id}/run", response_model=None)
    async def run_job_now(job_id: str) -> dict[str, Any]:
        """Trigger immediate execution of a cron job."""
        try:
            scheduler = server.services.cron_scheduler
            if scheduler is not None:
                try:
                    run = await scheduler.run_now(job_id)
                except CronRunRejected as exc:
                    status_code = 409 if exc.code == "cron_job_already_running" else 429
                    raise HTTPException(
                        status_code=status_code,
                        detail={"code": exc.code, "message": str(exc)},
                    ) from exc
                if not run:
                    storage = _get_storage()
                    if await server.run_db(storage.get_job, job_id):
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "cron_job_already_running",
                                "message": f"Cron job already has an active run: {job_id}",
                            },
                        )
                    raise HTTPException(status_code=404, detail=f"Cron job not found: {job_id}")
                return {"status": "success", "run": run.to_dict()}

            raise HTTPException(
                status_code=503,
                detail={
                    "code": "cron_scheduler_unavailable",
                    "message": "Cron scheduler is not available",
                },
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error running cron job: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.get("/jobs/{job_id}/runs")
    async def list_runs(
        job_id: str,
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        """List run history for a cron job."""
        try:
            storage = _get_storage()
            runs = await server.run_db(storage.list_runs, job_id, limit=limit)
            return {
                "status": "success",
                "runs": [r.to_dict() for r in runs],
                "count": len(runs),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error listing cron runs: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        """Get a specific cron run by ID."""
        try:
            storage = _get_storage()
            run = await server.run_db(storage.get_run, run_id)
            if not run:
                raise HTTPException(status_code=404, detail=f"Cron run not found: {run_id}")
            return {"status": "success", "run": run.to_dict()}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting cron run: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error") from e

    return router

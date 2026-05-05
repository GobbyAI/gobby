"""Build automation API routes."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from gobby.build import (
    BuildControlResult,
    BuildOptions,
    BuildResult,
    BuildTargetControlResult,
    build,
    build_clean_target,
    build_restart_target,
    build_resume,
    build_resume_target,
    build_stop,
    build_stop_target,
)
from gobby.config.build import Isolation
from gobby.config.build import StageCapOverride as BuildStageCapOverride

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


class BuildRequest(BaseModel):
    """Request body for POST /api/build."""

    model_config = ConfigDict(extra="forbid")

    input_ref: str
    quick: bool = False
    skip_stages: list[str] = Field(default_factory=list)
    isolation: Isolation | None = None
    no_merge: bool = False
    pr: str | None = None
    stage: list[str] = Field(default_factory=list)
    target_branch: str | None = None
    agent: str | None = None
    clones_dir: str | None = None
    reset_expansion_output: bool = False
    max_active_agents: int | None = Field(default=None, ge=1)


class BuildControlRequest(BaseModel):
    """Request body for POST /api/build/{stop,resume,clean,restart}."""

    input_ref: str | None = None
    dry_run: bool = False
    force: bool = False
    yes: bool = False


def _build_options(request_data: BuildRequest) -> BuildOptions:
    clones_dir = Path(request_data.clones_dir).expanduser() if request_data.clones_dir else None
    return BuildOptions(
        quick=request_data.quick,
        skip_stages=request_data.skip_stages,
        isolation=request_data.isolation or "worktree",
        isolation_explicit=request_data.isolation is not None,
        no_merge=request_data.no_merge,
        pr=request_data.pr,
        stage_caps=_parse_stage_options(request_data.stage),
        target_branch=request_data.target_branch,
        assigned_agent=request_data.agent,
        clones_dir=clones_dir,
        reset_expansion_output=request_data.reset_expansion_output,
        max_active_agents=request_data.max_active_agents,
    )


def _parse_stage_options(values: list[str]) -> list[BuildStageCapOverride]:
    parsed: dict[str, dict[str, int | None]] = {}
    for raw in values:
        stage_name, separator, settings_text = raw.partition(":")
        stage_name = stage_name.strip()
        if not stage_name:
            raise ValueError("stage name is required")
        settings = parsed.setdefault(stage_name, {})
        if not separator:
            continue
        for item in (part.strip() for part in settings_text.split(",") if part.strip()):
            key, key_separator, value_text = item.partition("=")
            if not key_separator:
                raise ValueError("stage setting must use name=value")
            key = key.strip()
            if key not in {"max_work_attempts", "max_review_rounds"}:
                raise ValueError("stage setting must be max_work_attempts or max_review_rounds")
            try:
                settings[key] = int(value_text)
            except ValueError as exc:
                raise ValueError("stage setting value must be an integer") from exc
    return [
        BuildStageCapOverride(
            stage_name=stage_name,
            max_work_attempts=settings.get("max_work_attempts"),
            max_review_rounds=settings.get("max_review_rounds"),
        )
        for stage_name, settings in parsed.items()
    ]


def _build_result_json(result: BuildResult) -> dict[str, Any]:
    payload = asdict(result)
    dispatcher_tick = payload.get("dispatcher_tick")
    if (
        isinstance(dispatcher_tick, dict)
        and dispatcher_tick.get("reason") == "dispatcher_cron_disabled"
    ):
        payload["dispatcher_cron_disabled"] = True
        payload["message"] = (
            "dispatcher_cron_disabled: dispatcher cron is disabled. "
            "Run `gobby build resume` to re-enable build automation."
        )
    return payload


def _result_json(result: BuildControlResult | BuildTargetControlResult) -> dict[str, Any]:
    if isinstance(result, BuildTargetControlResult):
        return result.to_dict()
    return asdict(result)


def create_build_router(server: HTTPServer) -> APIRouter:
    """Create the build API router."""
    router = APIRouter(prefix="/api/build", tags=["build"])

    @router.post("")
    async def post_build(request_data: BuildRequest) -> dict[str, Any]:
        """Start lifecycle automation for a plan, epic, or automated leaf task."""
        try:
            project_id = server.resolve_project_id(project_id=None, cwd=None)
            result = await build(
                request_data.input_ref,
                _build_options(request_data),
                db=server.services.database,
                project_id=project_id,
                services=server.services,
            )
            return _build_result_json(result)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/stop")
    async def post_build_stop(request_data: BuildControlRequest) -> dict[str, Any]:
        """Stop project-wide dispatcher ticks or task-scoped automation."""
        try:
            project_id = server.resolve_project_id(project_id=None, cwd=None)
            if request_data.input_ref is None:
                return _result_json(build_stop(db=server.services.database, project_id=project_id))
            result = await build_stop_target(
                request_data.input_ref,
                db=server.services.database,
                project_id=project_id,
                services=server.services,
            )
            return _result_json(result)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/resume")
    async def post_build_resume(request_data: BuildControlRequest) -> dict[str, Any]:
        """Resume project-wide dispatcher ticks or task-scoped automation."""
        try:
            project_id = server.resolve_project_id(project_id=None, cwd=None)
            if request_data.input_ref is None:
                return _result_json(
                    build_resume(db=server.services.database, project_id=project_id)
                )
            result = await build_resume_target(
                request_data.input_ref,
                db=server.services.database,
                project_id=project_id,
                services=server.services,
            )
            return _result_json(result)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/clean")
    async def post_build_clean(request_data: BuildControlRequest) -> dict[str, Any]:
        """Delete failed build artifacts for a task ref."""
        if request_data.input_ref is None:
            raise HTTPException(status_code=400, detail="input_ref is required")
        try:
            project_id = server.resolve_project_id(project_id=None, cwd=None)
            result = await build_clean_target(
                request_data.input_ref,
                db=server.services.database,
                project_id=project_id,
                dry_run=request_data.dry_run,
                force=request_data.force,
                yes=request_data.yes,
                services=server.services,
            )
            return result.to_dict()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/restart")
    async def post_build_restart(request_data: BuildControlRequest) -> dict[str, Any]:
        """Stop, clean, and resume task-scoped build automation."""
        if request_data.input_ref is None:
            raise HTTPException(status_code=400, detail="input_ref is required")
        try:
            project_id = server.resolve_project_id(project_id=None, cwd=None)
            result = await build_restart_target(
                request_data.input_ref,
                db=server.services.database,
                project_id=project_id,
                dry_run=request_data.dry_run,
                force=request_data.force,
                yes=request_data.yes,
                services=server.services,
            )
            return result.to_dict()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    return router

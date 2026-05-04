"""Build automation API routes."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from gobby.build import (
    BuildControlResult,
    BuildOptions,
    BuildResult,
    BuildTargetControlResult,
    StageInsertion,
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


class StageCapOverride(BaseModel):
    """Per-stage build cap override."""

    stage_name: str
    max_work_attempts: int | None = None
    max_review_rounds: int | None = None


class BuildRequest(BaseModel):
    """Request body for POST /api/build."""

    input_ref: str
    profile: str | None = None
    skip_stages: list[str] = Field(default_factory=list)
    stages: list[str] | None = None
    add_stages: list[str] = Field(default_factory=list)
    isolation: Isolation = "worktree"
    unattended: bool = False
    composer_yolo: bool = False
    yolo: bool | None = Field(default=None, json_schema_extra={"deprecated": True})
    stage_caps: list[StageCapOverride] = Field(default_factory=list)
    target_branch: str | None = None
    agent: str | None = None
    clones_dir: str | None = None
    reset_expansion_output: bool = False


class BuildControlRequest(BaseModel):
    """Request body for POST /api/build/{stop,resume,clean,restart}."""

    input_ref: str | None = None
    dry_run: bool = False
    force: bool = False
    yes: bool = False


def _build_options(request_data: BuildRequest) -> BuildOptions:
    clones_dir = Path(request_data.clones_dir).expanduser() if request_data.clones_dir else None
    unattended = request_data.unattended
    if request_data.yolo is not None and not unattended:
        unattended = request_data.yolo
    try:
        add_stages = [_parse_stage_insertion(value) for value in request_data.add_stages]
    except ValueError as exc:
        raise ValueError(f"Invalid stage insertion: {exc}") from exc
    return BuildOptions(
        profile=request_data.profile,
        skip_stages=request_data.skip_stages,
        isolation=request_data.isolation,
        unattended=unattended,
        composer_yolo=request_data.composer_yolo,
        stages=request_data.stages,
        add_stages=add_stages,
        stage_caps=[
            BuildStageCapOverride(
                stage_name=item.stage_name,
                max_work_attempts=item.max_work_attempts,
                max_review_rounds=item.max_review_rounds,
            )
            for item in request_data.stage_caps
        ],
        target_branch=request_data.target_branch,
        assigned_agent=request_data.agent,
        clones_dir=clones_dir,
        reset_expansion_output=request_data.reset_expansion_output,
    )


def _parse_stage_insertion(value: str) -> StageInsertion:
    stage_name, separator, position_text = value.partition("@")
    stage_name = stage_name.strip()
    if not stage_name:
        raise ValueError("stage name is required")
    position = None
    if separator:
        position_text = position_text.strip()
        if not position_text:
            raise ValueError("stage insertion position is required")
        try:
            position = int(position_text)
        except ValueError as exc:
            raise ValueError("stage insertion position must be an integer") from exc
    return StageInsertion(stage_name=stage_name, position=position)


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

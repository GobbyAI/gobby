"""Build automation API routes."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
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
from gobby.build.dispatch_tick import kick_dispatcher_tick as _kick_dispatcher_tick
from gobby.build.observability import (
    explain_dispatch,
    get_build_status,
    list_build_history,
)
from gobby.build.options import resolve_build_isolation
from gobby.build.profiles import BuildProfileError
from gobby.config.build import StageCapOverride as BuildStageCapOverride

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


class BuildRequest(BaseModel):
    """Request body for POST /api/build."""

    model_config = ConfigDict(extra="forbid")

    input_ref: str
    profile: str | None = None
    quick: bool = False
    skip_stages: list[str] = Field(default_factory=list)
    workspace_backend: Literal["worktree", "clone"] | None = None
    isolation: Literal["none", "worktree", "clone"] | None = None
    clone: bool = False
    unattended: bool | None = None
    no_merge: bool = False
    pr: str | None = None
    stage: list[str] = Field(default_factory=list)
    target_branch: str | None = None
    agent: str | None = None
    clones_dir: str | None = None
    reset_expansion_output: bool = False
    max_active_agents: int | None = Field(default=None, ge=1)
    max_retries: int | None = Field(default=None, ge=0)
    planning_seed_state: Literal["drafted", "needs_review", "approved"] = "drafted"
    completed_plan_review_rounds: int = Field(default=0, ge=0)


class BuildControlRequest(BaseModel):
    """Request body for POST /api/build/{stop,resume,clean,restart}."""

    input_ref: str | None = None
    dry_run: bool = False
    force: bool = False
    yes: bool = False
    no_resume: bool = False


def _build_options(request_data: BuildRequest) -> BuildOptions:
    clones_dir = Path(request_data.clones_dir).expanduser() if request_data.clones_dir else None
    isolation = resolve_build_isolation(
        isolation=request_data.isolation,
        workspace_backend=request_data.workspace_backend,
        clone=request_data.clone,
    )
    return BuildOptions(
        profile=request_data.profile or "default",
        profile_explicit="profile" in request_data.model_fields_set,
        quick=request_data.quick,
        skip_stages=request_data.skip_stages,
        skip_stages_explicit="skip_stages" in request_data.model_fields_set,
        isolation=isolation.isolation,
        isolation_explicit=isolation.explicit,
        unattended=request_data.unattended if request_data.unattended is not None else False,
        unattended_explicit="unattended" in request_data.model_fields_set,
        no_merge=request_data.no_merge,
        pr=request_data.pr,
        stage_caps=_parse_stage_options(request_data.stage),
        target_branch=request_data.target_branch,
        assigned_agent=request_data.agent,
        clones_dir=clones_dir,
        reset_expansion_output=request_data.reset_expansion_output,
        max_active_agents=request_data.max_active_agents,
        max_retries=request_data.max_retries,
        planning_seed_state=request_data.planning_seed_state,
        completed_plan_review_rounds=request_data.completed_plan_review_rounds,
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


def _success_envelope(result: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "result": result, "error": None}


def _error_envelope(message: str, error_code: str) -> dict[str, Any]:
    return {"success": False, "result": None, "error": message, "error_code": error_code}


def _resume_result_json(
    result: BuildControlResult | BuildTargetControlResult,
    *,
    dispatcher_tick: Any | None = None,
) -> dict[str, Any]:
    payload = _result_json(result)
    if dispatcher_tick is not None:
        payload["dispatcher_tick"] = asdict(dispatcher_tick)
    _add_dispatch_summary(payload)
    return payload


def _add_dispatch_summary(payload: dict[str, Any]) -> None:
    tick = payload.get("dispatcher_tick")
    if not isinstance(tick, dict):
        return
    executed = tick.get("executed")
    executed_count = executed if isinstance(executed, int) else 0
    payload["dispatch"] = {
        "status": "dispatched" if executed_count > 0 else "no_op",
        "executed": executed_count,
        "scanned": tick.get("scanned", 0),
        "skipped": tick.get("skipped", 0),
        "reason": tick.get("reason"),
        "cap_reached": tick.get("cap_reached", False),
    }


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
        except BuildProfileError as e:
            raise HTTPException(
                status_code=400,
                detail={"message": str(e), "error_code": "BUILD_PROFILE_ERROR"},
                headers={"X-Error-Type": "build_profile"},
            ) from e
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

    @router.post("/resume", response_model=None)
    async def post_build_resume(request_data: BuildControlRequest) -> dict[str, Any] | JSONResponse:
        """Resume project-wide dispatcher ticks or task-scoped automation."""
        try:
            project_id = server.resolve_project_id(project_id=None, cwd=None)
            if request_data.input_ref is None:
                result = build_resume(db=server.services.database, project_id=project_id)
                tick = await _kick_dispatcher_tick(
                    server.services.database,
                    project_id,
                    services=server.services,
                )
                return _success_envelope(_resume_result_json(result, dispatcher_tick=tick))
            target_result = await build_resume_target(
                request_data.input_ref,
                db=server.services.database,
                project_id=project_id,
                services=server.services,
            )
            return _success_envelope(_resume_result_json(target_result))
        except ValueError as e:
            return JSONResponse(
                status_code=400,
                content=_error_envelope(str(e), "BUILD_RESUME_ERROR"),
            )

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
                no_resume=request_data.no_resume,
                services=server.services,
            )
            return result.to_dict()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.get("/status")
    async def get_status(
        input_ref: str,
        history_limit: int = Query(5, ge=1, le=100),
    ) -> dict[str, Any]:
        """Return compact build status for a task tree or build input."""
        try:
            project_id = server.resolve_project_id(project_id=None, cwd=None)
            return await asyncio.to_thread(
                get_build_status,
                input_ref,
                db=server.services.database,
                project_id=project_id,
                history_limit=history_limit,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.get("/dispatch/explain")
    async def get_dispatch_explain(
        task_id: str,
        max_active_agents: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        """Explain dispatcher eligibility and proposed action without mutation."""
        try:
            project_id = server.resolve_project_id(project_id=None, cwd=None)
            return await asyncio.to_thread(
                explain_dispatch,
                task_id,
                db=server.services.database,
                project_id=project_id,
                max_active_agents=max_active_agents,
                services=server.services,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.get("/history")
    async def get_history(
        input_ref: str,
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        """List recent build run and event rows."""
        try:
            project_id = server.resolve_project_id(project_id=None, cwd=None)
            return await asyncio.to_thread(
                list_build_history,
                input_ref,
                db=server.services.database,
                project_id=project_id,
                limit=limit,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    return router

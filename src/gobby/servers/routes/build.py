"""Build automation API routes."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from gobby.build import BuildOptions, BuildResult, build
from gobby.config.build import Isolation

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


class BuildRequest(BaseModel):
    """Request body for POST /api/build."""

    input_ref: str
    profile: str | None = None
    skip_stages: list[str] = Field(default_factory=list)
    isolation: Isolation = "worktree"
    unattended: bool = False
    composer_yolo: bool = False
    yolo: bool | None = Field(default=None, json_schema_extra={"deprecated": True})
    max_review_rounds: int = 3
    max_expansion_attempts: int | None = None
    max_qa_rounds: int | None = None
    max_merge_attempts: int | None = None
    max_holistic_rounds: int | None = None
    target_branch: str | None = None
    agent: str | None = None
    clones_dir: str | None = None


def _build_options(request_data: BuildRequest) -> BuildOptions:
    clones_dir = Path(request_data.clones_dir).expanduser() if request_data.clones_dir else None
    unattended = request_data.unattended
    if request_data.yolo is not None and not unattended:
        unattended = request_data.yolo
    return BuildOptions(
        profile=request_data.profile,
        skip_stages=request_data.skip_stages,
        isolation=request_data.isolation,
        unattended=unattended,
        composer_yolo=request_data.composer_yolo,
        max_review_rounds=request_data.max_review_rounds,
        max_expansion_attempts=request_data.max_expansion_attempts,
        max_qa_rounds=request_data.max_qa_rounds,
        max_merge_attempts=request_data.max_merge_attempts,
        max_holistic_rounds=request_data.max_holistic_rounds,
        target_branch=request_data.target_branch,
        assigned_agent=request_data.agent,
        clones_dir=clones_dir,
    )


def _build_result_json(result: BuildResult) -> dict[str, Any]:
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
            )
            return _build_result_json(result)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    return router

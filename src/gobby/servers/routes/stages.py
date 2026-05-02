"""Stage registry HTTP routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gobby.storage.tasks._stage_views import stage_registry_entry_view

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer


class StageRegistryEntryView(BaseModel):
    name: str
    display_label: str
    description: str
    category: Literal["discovery", "design", "verification", "implementation", "delivery"]
    default_agent: str | None
    reviewer_agent: str | None
    review_policy: Literal["none", "required", "optional"]
    position_hint: int
    requires_human: bool
    is_terminal: bool
    default_max_work_attempts: int
    default_max_review_rounds: int


class StagesRegistryResponse(BaseModel):
    stages: list[StageRegistryEntryView]


class TaskTypeDefaultStageView(BaseModel):
    stage_name: str
    position: int


class TaskTypeDefaultsResponse(BaseModel):
    task_type: str
    stages: list[TaskTypeDefaultStageView]


router = APIRouter(tags=["stages"])


def create_stages_router(server: HTTPServer) -> APIRouter:
    """Create stage registry routes bound to the server's task manager."""
    stage_router = APIRouter(tags=["stages"])

    @stage_router.get("/api/stages/registry", response_model=StagesRegistryResponse)
    async def list_stages_registry() -> dict[str, object]:
        entries = server.task_manager.stages_registry.list_all()
        return {"stages": [stage_registry_entry_view(entry) for entry in entries]}

    @stage_router.get(
        "/api/task-types/{task_type}/default-stages",
        response_model=TaskTypeDefaultsResponse,
    )
    async def get_task_type_defaults(task_type: str) -> dict[str, object]:
        try:
            stages = server.task_manager.stages_registry.list_default_stages(task_type)
            return {
                "task_type": task_type,
                "stages": [
                    {"stage_name": stage_name, "position": position}
                    for stage_name, position in stages
                ],
            }
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    return stage_router

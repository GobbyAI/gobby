"""Stage registry HTTP routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

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
    reviewer_agent_selector_json: str | None
    review_policy: Literal["none", "required", "optional"]
    dispatch_type: Literal["agent", "pipeline"] | None
    dispatch_target: str | None
    dispatch_inputs_json: str | None
    position_hint: int
    requires_human: bool
    is_terminal: bool
    default_max_work_attempts: int
    default_max_review_rounds: int
    bundled_hash: str | None = None
    deleted_at: str | None = None
    is_edited: bool = False


class StagesRegistryResponse(BaseModel):
    stages: list[StageRegistryEntryView]


class TaskTypeDefaultStageView(BaseModel):
    stage_name: str
    position: int


class TaskTypeDefaultsResponse(BaseModel):
    task_type: str
    stages: list[TaskTypeDefaultStageView]


class StageUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_label: str | None = None
    description: str | None = None
    category: (
        Literal["discovery", "design", "verification", "implementation", "delivery"] | None
    ) = None
    default_agent: str | None = None
    reviewer_agent: str | None = None
    reviewer_agent_selector_json: str | None = None
    review_policy: Literal["none", "required", "optional"] | None = None
    dispatch_type: Literal["agent", "pipeline"] | None = None
    dispatch_target: str | None = None
    dispatch_inputs_json: str | None = None
    position_hint: int | None = None
    requires_human: bool | None = None
    is_terminal: bool | None = None
    default_max_work_attempts: int | None = None
    default_max_review_rounds: int | None = None


class SetTaskTypeDefaultsRequest(BaseModel):
    stages: list[TaskTypeDefaultStageView]


router = APIRouter(tags=["stages"])


def create_stages_router(server: HTTPServer) -> APIRouter:
    """Create stage registry routes bound to the server's task manager."""
    stage_router = APIRouter(tags=["stages"])

    @stage_router.get("/api/stages/registry", response_model=StagesRegistryResponse)
    async def list_stages_registry(include_deleted: bool = False) -> dict[str, object]:
        entries = server.task_manager.stages_registry.list_all(include_deleted=include_deleted)
        return {"stages": [stage_registry_entry_view(entry) for entry in entries]}

    @stage_router.put("/api/stages/registry/{name}", response_model=StageRegistryEntryView)
    async def update_stage(name: str, request_data: StageUpdateRequest) -> dict[str, object]:
        try:
            entry = server.task_manager.stages_registry.update_stage(
                name,
                request_data.model_dump(exclude_unset=True),
            )
            return stage_registry_entry_view(entry)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @stage_router.post("/api/stages/registry/{name}/restore", response_model=StageRegistryEntryView)
    async def restore_stage(name: str) -> dict[str, object]:
        try:
            return stage_registry_entry_view(
                server.task_manager.stages_registry.restore_stage(name)
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @stage_router.delete("/api/stages/registry/{name}", response_model=StageRegistryEntryView)
    async def delete_stage(name: str) -> dict[str, object]:
        try:
            return stage_registry_entry_view(server.task_manager.stages_registry.delete_stage(name))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

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

    @stage_router.put(
        "/api/task-types/{task_type}/default-stages",
        response_model=TaskTypeDefaultsResponse,
    )
    async def set_task_type_defaults(
        task_type: str,
        request_data: SetTaskTypeDefaultsRequest,
    ) -> dict[str, object]:
        try:
            stages = [(item.stage_name, item.position) for item in request_data.stages]
            server.task_manager.stages_registry.set_default_stages(task_type, stages)
            return {
                "task_type": task_type,
                "stages": [
                    {"stage_name": stage_name, "position": position}
                    for stage_name, position in server.task_manager.stages_registry.list_default_stages(
                        task_type
                    )
                ],
            }
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    return stage_router

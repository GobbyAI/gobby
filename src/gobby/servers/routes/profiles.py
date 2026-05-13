"""Build profile HTTP routes."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from gobby.config.build import DeliveryMode, Isolation
from gobby.storage.build_profiles import BuildProfileError, BuildProfileManager

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

ProfileSource = Literal["installed", "project"]


class ProfileCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_label: str
    description: str
    skip_stages: list[str] = Field(default_factory=list)
    isolation: Isolation = "worktree"
    unattended: bool = False
    delivery_mode: DeliveryMode = "auto"
    delivery_target_repo: str | None = None
    enabled: bool = True
    source: ProfileSource = "project"
    project_id: str | None = None
    tags: list[str] = Field(default_factory=list)


class ProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_label: str | None = None
    description: str | None = None
    skip_stages: list[str] | None = None
    isolation: Isolation | None = None
    unattended: bool | None = None
    delivery_mode: DeliveryMode | None = None
    delivery_target_repo: str | None = None
    enabled: bool | None = None
    tags: list[str] | None = None


def create_profiles_router(server: HTTPServer) -> APIRouter:
    """Create build profile routes."""

    router = APIRouter(prefix="/api/profiles", tags=["profiles"])

    def manager() -> BuildProfileManager:
        return BuildProfileManager(server.services.database)

    def scope_project_id(source: ProfileSource, project_id: str | None) -> str | None:
        if source == "installed":
            return None
        return project_id or server.resolve_project_id(project_id=None, cwd=None)

    @router.get("")
    async def list_profiles(
        include_deleted: bool = False,
        project_id: str | None = None,
    ) -> dict[str, object]:
        resolved_project_id = project_id or server.resolve_project_id(project_id=None, cwd=None)
        profiles = manager().list_profiles(
            project_id=resolved_project_id,
            include_deleted=include_deleted,
        )
        return {"profiles": [asdict(profile) for profile in profiles]}

    @router.get("/{name}")
    async def show_profile(
        name: str,
        source: ProfileSource = "installed",
        project_id: str | None = None,
        include_deleted: bool = False,
    ) -> dict[str, object]:
        profile = manager().get(
            name,
            source=source,
            project_id=scope_project_id(source, project_id),
            include_deleted=include_deleted,
        )
        if profile is None:
            raise HTTPException(status_code=404, detail=f"Unknown build profile '{name}'")
        return asdict(profile)

    @router.post("")
    async def create_profile(request_data: ProfileCreateRequest) -> dict[str, object]:
        try:
            profile = manager().create(
                name=request_data.name,
                display_label=request_data.display_label,
                description=request_data.description,
                skip_stages=request_data.skip_stages,
                isolation=request_data.isolation,
                unattended=request_data.unattended,
                delivery_mode=request_data.delivery_mode,
                delivery_target_repo=request_data.delivery_target_repo,
                enabled=request_data.enabled,
                source=request_data.source,
                project_id=scope_project_id(request_data.source, request_data.project_id),
                tags=request_data.tags,
            )
            return asdict(profile)
        except BuildProfileError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.put("/{name}")
    async def update_profile(
        name: str,
        request_data: ProfileUpdateRequest,
        source: ProfileSource = "project",
        project_id: str | None = None,
    ) -> dict[str, object]:
        try:
            profile = manager().update(
                name,
                source=source,
                project_id=scope_project_id(source, project_id),
                updates=request_data.model_dump(exclude_unset=True),
            )
            return asdict(profile)
        except BuildProfileError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/{name}/restore")
    async def restore_profile(
        name: str,
        source: ProfileSource = "installed",
        project_id: str | None = None,
    ) -> dict[str, object]:
        try:
            profile = manager().restore(
                name,
                source=source,
                project_id=scope_project_id(source, project_id),
            )
            return asdict(profile)
        except BuildProfileError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.delete("/{name}")
    async def delete_profile(
        name: str,
        source: ProfileSource = "project",
        project_id: str | None = None,
        purge: bool = False,
    ) -> dict[str, object]:
        try:
            profile = manager().delete(
                name,
                source=source,
                project_id=scope_project_id(source, project_id),
                purge=purge,
            )
            return {"profile": asdict(profile) if profile is not None else None}
        except BuildProfileError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post("/{name}/enable")
    async def enable_profile(
        name: str,
        source: ProfileSource = "project",
        project_id: str | None = None,
    ) -> dict[str, object]:
        return await _set_enabled(name, source, project_id, enabled=True)

    @router.post("/{name}/disable")
    async def disable_profile(
        name: str,
        source: ProfileSource = "project",
        project_id: str | None = None,
    ) -> dict[str, object]:
        return await _set_enabled(name, source, project_id, enabled=False)

    async def _set_enabled(
        name: str,
        source: ProfileSource,
        project_id: str | None,
        *,
        enabled: bool,
    ) -> dict[str, object]:
        try:
            profile = manager().set_enabled(
                name,
                source=source,
                project_id=scope_project_id(source, project_id),
                enabled=enabled,
            )
            return asdict(profile)
        except BuildProfileError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    return router

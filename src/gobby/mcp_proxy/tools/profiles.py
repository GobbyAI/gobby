"""MCP tools for build profile registry editing."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from gobby.config.build import Isolation
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.build_profiles import BuildProfileManager, BuildProfileSource
from gobby.storage.database import DatabaseProtocol


def create_profiles_registry(
    db: DatabaseProtocol,
    *,
    default_project_id: str | None = None,
) -> InternalToolRegistry:
    """Create the build profile registry MCP surface."""

    registry = InternalToolRegistry(
        name="gobby-profiles",
        description="Build profile registry tools",
    )
    manager = BuildProfileManager(db)

    def _scope_project_id(source: BuildProfileSource, project_id: str | None) -> str | None:
        if source == "installed":
            return None
        return project_id if project_id is not None else default_project_id

    def list_profiles(
        include_deleted: bool = False,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        profiles = manager.list_profiles(
            project_id=project_id if project_id is not None else default_project_id,
            include_deleted=include_deleted,
        )
        return {"ok": True, "profiles": [asdict(profile) for profile in profiles]}

    registry.register(
        name="list_profiles",
        description="List build profiles.",
        input_schema={
            "type": "object",
            "properties": {
                "include_deleted": {"type": "boolean", "default": False},
                "project_id": {"type": ["string", "null"]},
            },
            "required": [],
        },
        output_schema={"type": "object"},
        func=list_profiles,
    )

    def show_profile(
        name: str,
        source: BuildProfileSource = "installed",
        project_id: str | None = None,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        profile = manager.get(
            name,
            source=source,
            project_id=_scope_project_id(source, project_id),
            include_deleted=include_deleted,
        )
        if profile is None:
            return {"ok": False, "error": "not_found", "message": f"Unknown profile '{name}'"}
        return {"ok": True, "profile": asdict(profile)}

    registry.register(
        name="show_profile",
        description="Show one build profile.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "source": {"type": "string", "enum": ["installed", "project"]},
                "project_id": {"type": ["string", "null"]},
                "include_deleted": {"type": "boolean", "default": False},
            },
            "required": ["name"],
        },
        output_schema={"type": "object"},
        func=show_profile,
    )

    def create_profile(
        name: str,
        display_label: str,
        description: str,
        skip_stages: list[str] | None = None,
        isolation: Isolation = "worktree",
        unattended: bool = False,
        enabled: bool = True,
        source: BuildProfileSource = "project",
        project_id: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        profile = manager.create(
            name=name,
            display_label=display_label,
            description=description,
            skip_stages=skip_stages or [],
            isolation=isolation,
            unattended=unattended,
            enabled=enabled,
            source=source,
            project_id=_scope_project_id(source, project_id),
            tags=tags,
        )
        return {"ok": True, "profile": asdict(profile)}

    registry.register(
        name="create_profile",
        description="Create a build profile.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "display_label": {"type": "string"},
                "description": {"type": "string"},
                "skip_stages": {"type": "array", "items": {"type": "string"}},
                "isolation": {"type": "string", "enum": ["none", "worktree", "clone"]},
                "unattended": {"type": "boolean"},
                "enabled": {"type": "boolean"},
                "source": {"type": "string", "enum": ["installed", "project"]},
                "project_id": {"type": ["string", "null"]},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name", "display_label", "description"],
        },
        output_schema={"type": "object"},
        func=create_profile,
    )

    def update_profile(
        name: str,
        updates: dict[str, Any],
        source: BuildProfileSource = "project",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        profile = manager.update(
            name,
            source=source,
            project_id=_scope_project_id(source, project_id),
            updates=updates,
        )
        return {"ok": True, "profile": asdict(profile)}

    registry.register(
        name="update_profile",
        description="Update a build profile. Profile names are immutable.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "updates": {"type": "object"},
                "source": {"type": "string", "enum": ["installed", "project"]},
                "project_id": {"type": ["string", "null"]},
            },
            "required": ["name", "updates"],
        },
        output_schema={"type": "object"},
        func=update_profile,
    )

    def restore_profile(
        name: str,
        source: BuildProfileSource = "installed",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        profile = manager.restore(
            name,
            source=source,
            project_id=_scope_project_id(source, project_id),
        )
        return {"ok": True, "profile": asdict(profile)}

    registry.register(
        name="restore_profile",
        description="Restore a bundled build profile.",
        input_schema=_profile_scope_schema(required=["name"]),
        output_schema={"type": "object"},
        func=restore_profile,
    )

    def delete_profile(
        name: str,
        source: BuildProfileSource = "project",
        project_id: str | None = None,
        purge: bool = False,
    ) -> dict[str, Any]:
        profile = manager.delete(
            name,
            source=source,
            project_id=_scope_project_id(source, project_id),
            purge=purge,
        )
        return {"ok": True, "profile": asdict(profile) if profile is not None else None}

    registry.register(
        name="delete_profile",
        description="Soft-delete a build profile, or purge a project profile.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "source": {"type": "string", "enum": ["installed", "project"]},
                "project_id": {"type": ["string", "null"]},
                "purge": {"type": "boolean", "default": False},
            },
            "required": ["name"],
        },
        output_schema={"type": "object"},
        func=delete_profile,
    )

    def enable_profile(
        name: str,
        source: BuildProfileSource = "project",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        profile = manager.set_enabled(
            name,
            source=source,
            project_id=_scope_project_id(source, project_id),
            enabled=True,
        )
        return {"ok": True, "profile": asdict(profile)}

    def disable_profile(
        name: str,
        source: BuildProfileSource = "project",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        profile = manager.set_enabled(
            name,
            source=source,
            project_id=_scope_project_id(source, project_id),
            enabled=False,
        )
        return {"ok": True, "profile": asdict(profile)}

    registry.register(
        name="enable_profile",
        description="Enable a build profile.",
        input_schema=_profile_scope_schema(required=["name"]),
        output_schema={"type": "object"},
        func=enable_profile,
    )
    registry.register(
        name="disable_profile",
        description="Disable a build profile.",
        input_schema=_profile_scope_schema(required=["name"]),
        output_schema={"type": "object"},
        func=disable_profile,
    )

    return registry


def _profile_scope_schema(*, required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "source": {"type": "string", "enum": ["installed", "project"]},
            "project_id": {"type": ["string", "null"]},
        },
        "required": required,
    }


__all__ = ["create_profiles_registry"]

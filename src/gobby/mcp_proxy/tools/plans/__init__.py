"""Internal MCP tools for DB-backed plan management."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.plans import LocalPlanManager, PlanNotFoundError
from gobby.storage.projects import LocalProjectManager


def create_plan_registry(
    db: HubDatabase, *, default_project_id: str | None = None
) -> InternalToolRegistry:
    """Create the gobby-plans registry."""

    registry = InternalToolRegistry(
        name="gobby-plans",
        description="Plan management - DB-backed plan registry and coverage manifests",
    )
    manager = LocalPlanManager(db)

    def create_plan(
        plan_id: str,
        plan_path: str,
        plan_kind: str = "implementation",
        root_task_ref: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        allowed_plan_kinds = {"implementation", "strategy"}
        if plan_kind not in allowed_plan_kinds:
            return {
                "ok": False,
                "error": "invalid_plan_kind",
                "message": (f"plan_kind must be one of: {', '.join(sorted(allowed_plan_kinds))}"),
            }
        project_id = _resolve_project_id(db, project, default_project_id)
        root_ref = root_task_ref or _root_task_from_path(plan_path)
        if root_ref is None:
            return {"ok": False, "error": "missing_root_task_ref"}
        try:
            record = manager.create_plan(
                project_id=project_id,
                plan_id=plan_id,
                plan_path=plan_path,
                plan_kind=plan_kind,
                root_task_ref=root_ref,
            )
        except (ValueError, OSError, sqlite3.Error) as exc:
            return {"ok": False, "error": "create_plan_failed", "message": str(exc)}
        return {"ok": True, "plan": record.to_dict()}

    registry.register(
        name="create_plan",
        description="Register a new plan and emit its initial coverage manifest.",
        input_schema={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "plan_path": {"type": "string"},
                "plan_kind": {"type": "string", "enum": ["implementation", "strategy"]},
                "root_task_ref": {"type": "string"},
                "project": {"type": "string"},
            },
            "required": ["plan_id", "plan_path"],
        },
        func=create_plan,
    )

    def get_plan(plan_id_or_ref: str, project: str | None = None) -> dict[str, Any]:
        try:
            record = manager.get_plan(
                plan_id_or_ref,
                project_id=_optional_project_id(db, project, default_project_id),
            )
        except PlanNotFoundError as exc:
            return {"ok": False, "error": "plan_not_found", "message": str(exc)}
        except ValueError as exc:
            return _known_error_payload(exc, "get_plan_failed")
        return {"ok": True, "plan": record.to_dict()}

    registry.register(
        name="get_plan",
        description="Get a plan row by plan_id or root task ref.",
        input_schema={
            "type": "object",
            "properties": {
                "plan_id_or_ref": {"type": "string"},
                "project": {"type": "string"},
            },
            "required": ["plan_id_or_ref"],
        },
        func=get_plan,
    )

    def list_plans(
        state: str | None = None,
        plan_kind: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        records = manager.list_plans(
            state=state,
            plan_kind=plan_kind,
            project_id=_optional_project_id(db, project, default_project_id),
        )
        return {
            "ok": True,
            "plans": [record.to_dict() for record in records],
            "count": len(records),
        }

    registry.register(
        name="list_plans",
        description="List plans with optional state, kind, and project filters.",
        input_schema={
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["active", "archived"]},
                "plan_kind": {"type": "string", "enum": ["implementation", "strategy"]},
                "project": {"type": "string"},
            },
        },
        func=list_plans,
    )

    def archive_plan(
        plan_id: str,
        reason: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        try:
            record = manager.archive_plan(
                plan_id,
                project_id=_optional_project_id(db, project, default_project_id),
                reason=reason,
            )
        except PlanNotFoundError as exc:
            return {"ok": False, "error": "plan_not_found", "message": str(exc)}
        except (ValueError, OSError, sqlite3.Error) as exc:
            return _known_error_payload(exc, "archive_plan_failed")
        return {"ok": True, "plan": record.to_dict()}

    registry.register(
        name="archive_plan",
        description="Archive a plan, move its file to completed, and remove its coverage manifest.",
        input_schema={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "reason": {"type": "string"},
                "project": {"type": "string"},
            },
            "required": ["plan_id"],
        },
        func=archive_plan,
    )

    def update_plan_hash(plan_id: str, project: str | None = None) -> dict[str, Any]:
        try:
            record = manager.update_plan_hash(
                plan_id,
                project_id=_optional_project_id(db, project, default_project_id),
            )
        except PlanNotFoundError as exc:
            return {"ok": False, "error": "plan_not_found", "message": str(exc)}
        except (ValueError, OSError, sqlite3.Error) as exc:
            return _known_error_payload(exc, "update_plan_hash_failed")
        return {"ok": True, "plan": record.to_dict()}

    registry.register(
        name="update_plan_hash",
        description="Recompute a plan hash and regenerate coverage if it changed.",
        input_schema={
            "type": "object",
            "properties": {"plan_id": {"type": "string"}, "project": {"type": "string"}},
            "required": ["plan_id"],
        },
        func=update_plan_hash,
    )

    def regenerate_coverage_manifest(plan_id: str, project: str | None = None) -> dict[str, Any]:
        try:
            path = manager.regenerate_coverage_manifest(
                plan_id,
                project_id=_optional_project_id(db, project, default_project_id),
            )
        except PlanNotFoundError as exc:
            return {"ok": False, "error": "plan_not_found", "message": str(exc)}
        except (ValueError, OSError, sqlite3.Error) as exc:
            return _known_error_payload(exc, "regenerate_manifest_failed")
        return {"ok": True, "manifest_path": str(path)}

    registry.register(
        name="regenerate_coverage_manifest",
        description="Regenerate the managed coverage manifest for a plan.",
        input_schema={
            "type": "object",
            "properties": {"plan_id": {"type": "string"}, "project": {"type": "string"}},
            "required": ["plan_id"],
        },
        func=regenerate_coverage_manifest,
    )

    def delete_plan(plan_id: str, project: str | None = None) -> dict[str, Any]:
        try:
            deleted = manager.delete_plan(
                plan_id,
                project_id=_optional_project_id(db, project, default_project_id),
            )
        except PlanNotFoundError as exc:
            return {"ok": False, "error": "plan_not_found", "message": str(exc)}
        except ValueError as exc:
            return {"ok": False, "error": "invalid_ref", "message": str(exc)}
        return {"ok": True, "deleted": deleted}

    registry.register(
        name="delete_plan",
        description="Hard-delete a plan row and remove its managed coverage manifest.",
        input_schema={
            "type": "object",
            "properties": {"plan_id": {"type": "string"}, "project": {"type": "string"}},
            "required": ["plan_id"],
        },
        func=delete_plan,
    )

    def validate_plan(plan_file: str) -> dict[str, Any]:
        from gobby.storage.tasks import LocalTaskManager
        from gobby.tasks.expansion._validate import validate_plan_file as _validate

        plan_path = Path(plan_file)
        if not plan_path.is_absolute():
            plan_path = Path.cwd() / plan_path
        # validate_plan_file is mounted as a method on ExpansionService for
        # convenience but its body never touches `self`; calling it as a free
        # function with `None` keeps gobby-plans free of the wider service
        # construction (LLM service, run manager, dep manager) it does not need.
        _ = LocalTaskManager  # imported for symmetry; not required for validation
        return _validate(None, plan_path)

    registry.register(
        name="validate_plan",
        description=(
            "Validate a plan file against the Plan-Coverage Contract. Mirrors "
            "gobby-tasks-ops:validate_plan_file so plan-related callers do not "
            "have to cross server boundaries."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "plan_file": {
                    "type": "string",
                    "description": "Plan file path (absolute or relative to cwd)",
                },
            },
            "required": ["plan_file"],
        },
        func=validate_plan,
    )

    return registry


def _resolve_project_id(
    db: HubDatabase, project: str | None, default_project_id: str | None
) -> str:
    project_id = _optional_project_id(db, project, default_project_id)
    if project_id is None:
        raise ValueError("project is required")
    return project_id


def _optional_project_id(
    db: HubDatabase,
    project: str | None,
    default_project_id: str | None,
) -> str | None:
    if project:
        resolved = LocalProjectManager(db).resolve_ref(project)
        return resolved.id if resolved is not None else None
    return default_project_id


def _known_error_payload(exc: Exception, fallback_error: str) -> dict[str, Any]:
    error = "invalid_ref" if "ref must not be blank" in str(exc) else fallback_error
    return {"ok": False, "error": error, "message": str(exc)}


def _root_task_from_path(plan_path: str) -> str | None:
    stem = Path(plan_path).stem
    if stem.startswith("task-"):
        token = stem.split("-", 2)[1]
        if token.isdecimal():
            return f"#{token}"
    return None


__all__ = ["create_plan_registry"]

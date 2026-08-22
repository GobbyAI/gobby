"""Internal MCP tools for DB-backed plan management."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

import psycopg

from gobby.code_index.storage import CodeIndexStorage
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.plans.review_evidence import register_review_evidence_tools
from gobby.storage.concurrency import CoverageExecutor
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.plans import LocalPlanManager, PlanNotFoundError, PlanRecord
from gobby.storage.projects import LocalProjectManager
from gobby.utils.project_context import get_project_context

P = ParamSpec("P")
T = TypeVar("T")
RunDb = Callable[..., Awaitable[Any]]


class _InvalidProjectError(ValueError):
    """Raised when an explicitly provided project reference cannot be resolved."""


def create_plan_registry(
    db: HubDatabase,
    *,
    default_project_id: str | None = None,
    run_db: RunDb | None = None,
    coverage_executor: CoverageExecutor | None = None,
) -> InternalToolRegistry:
    """Create the gobby-plans registry."""

    registry = InternalToolRegistry(
        name="gobby-plans",
        description="Plan management - DB-backed plan registry and coverage manifests",
    )
    manager = LocalPlanManager(db)
    code_index = CodeIndexStorage(db)

    async def create_plan(
        plan_id: str,
        plan_path: str,
        plan_kind: str = "implementation",
        root_task_ref: str | None = None,
        project: str | None = None,
        reactivate: bool = False,
    ) -> dict[str, Any]:
        allowed_plan_kinds = {"implementation", "strategy"}
        if plan_kind not in allowed_plan_kinds:
            return {
                "ok": False,
                "error": "invalid_plan_kind",
                "message": (f"plan_kind must be one of: {', '.join(sorted(allowed_plan_kinds))}"),
            }
        root_ref = root_task_ref or _root_task_from_path(plan_path)
        if root_ref is None:
            return {"ok": False, "error": "missing_root_task_ref"}

        def create_record() -> PlanRecord:
            project_id = _resolve_project_id(db, project, default_project_id)
            return manager.create_plan_record(
                project_id=project_id,
                plan_id=plan_id,
                plan_path=plan_path,
                plan_kind=plan_kind,
                root_task_ref=root_ref,
                reactivate=reactivate,
            )

        try:
            record = await _run_db_call(run_db, create_record)
            if plan_kind == "implementation":
                await _run_coverage(coverage_executor, manager.generate_coverage_manifest, record)
        except (ValueError, OSError, psycopg.Error) as exc:
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
                "reactivate": {"type": "boolean", "default": False},
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
        try:
            records = manager.list_plans(
                state=state,
                plan_kind=plan_kind,
                project_id=_optional_project_id(db, project, default_project_id),
            )
        except _InvalidProjectError as exc:
            return _known_error_payload(exc, "list_plans_failed")
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
        except (ValueError, OSError, psycopg.Error) as exc:
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

    async def update_plan_hash(plan_id: str, project: str | None = None) -> dict[str, Any]:
        def update_record() -> tuple[PlanRecord, bool]:
            return manager.update_plan_hash_record(
                plan_id,
                project_id=_optional_project_id(db, project, default_project_id),
            )

        try:
            record, changed = await _run_db_call(run_db, update_record)
            if changed and record.plan_kind == "implementation":
                await _run_coverage(coverage_executor, manager.generate_coverage_manifest, record)
        except PlanNotFoundError as exc:
            return {"ok": False, "error": "plan_not_found", "message": str(exc)}
        except (ValueError, OSError, psycopg.Error) as exc:
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

    async def regenerate_coverage_manifest(
        plan_id: str,
        project: str | None = None,
    ) -> dict[str, Any]:
        def get_record() -> PlanRecord:
            return manager.get_plan(
                plan_id,
                project_id=_optional_project_id(db, project, default_project_id),
            )

        try:
            record = await _run_db_call(run_db, get_record)
            path = await _run_coverage(
                coverage_executor,
                manager.generate_coverage_manifest,
                record,
            )
        except PlanNotFoundError as exc:
            return {"ok": False, "error": "plan_not_found", "message": str(exc)}
        except (ValueError, OSError, psycopg.Error) as exc:
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
            return _known_error_payload(exc, "invalid_ref")
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
        from gobby.tasks.expansion._validate import validate_plan_file as _validate

        project_context = get_project_context()
        context_project_id = (
            project_context.get("id") if project_context is not None else default_project_id
        )
        if isinstance(context_project_id, str) and (
            project_context is None or not project_context.get("project_path")
        ):
            project = LocalProjectManager(db).get(context_project_id)
            if project is not None and project.repo_path:
                project_context = {
                    "id": context_project_id,
                    "project_path": project.repo_path,
                    **(project_context or {}),
                }

        plan_path = Path(plan_file)
        context_path = project_context.get("project_path") if project_context is not None else None
        if isinstance(context_path, str) and context_path and not plan_path.is_absolute():
            plan_path = Path(context_path) / plan_path
        return _validate(
            None,
            plan_path,
            project_context=project_context,
            expected_project_id=default_project_id,
            code_index=code_index,
            require_symbol_validation=True,
        )

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
                    "description": (
                        "Plan file path (absolute or relative to the caller project root)"
                    ),
                },
            },
            "required": ["plan_file"],
        },
        func=validate_plan,
    )

    register_review_evidence_tools(
        registry,
        db,
        resolve_project_id=lambda project: _resolve_project_id(
            db,
            project,
            default_project_id,
        ),
    )
    return registry


async def _run_coverage(
    executor: CoverageExecutor | None,
    func: Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    if executor is None:
        raise RuntimeError("Plan coverage requires a configured CoverageExecutor")
    return await executor.run(func, *args, **kwargs)


async def _run_db_call(
    run_db: RunDb | None,
    func: Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    if run_db is None:
        return await asyncio.to_thread(func, *args, **kwargs)
    return cast(T, await run_db(func, *args, **kwargs))


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
        if resolved is None:
            raise _InvalidProjectError(f"Project not found: {project}")
        return resolved.id
    return default_project_id


def _known_error_payload(exc: Exception, fallback_error: str) -> dict[str, Any]:
    if isinstance(exc, _InvalidProjectError):
        error = "invalid_project"
    else:
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

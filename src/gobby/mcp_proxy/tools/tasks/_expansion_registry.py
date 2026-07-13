"""MCP registry wiring for task expansion tools."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._expansion_runtime import (
    _background_run_tasks,
    _build_expansion_service,
    _register_background_task,
    _summarize_run,
    start_expansion_run_impl,
)
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks import TaskNotFoundError
from gobby.tasks.expansion_qa_coverage import run_expansion_qa_coverage as run_qa_coverage
from gobby.utils.session_context import get_current_session_id

logger = logging.getLogger(__name__)

__all__ = ["create_expansion_registry"]


def _subscribe_completion(ctx: RegistryContext, run_id: str, resolved_session_id: str) -> None:
    """Register an expansion completion event for the calling session lineage."""
    if ctx.completion_registry is None:
        return

    lineage_ids: list[str] = [resolved_session_id]
    try:
        from gobby.agents.session import ChildSessionManager

        child_mgr = ChildSessionManager(ctx.session_manager)
        lineage = child_mgr.get_session_lineage(resolved_session_id)
        lineage_ids = [session.id for session in lineage]
        if resolved_session_id not in lineage_ids:
            lineage_ids.append(resolved_session_id)
    except Exception:
        logger.debug("Could not resolve session lineage for expansion completion", exc_info=True)

    try:
        if ctx.completion_registry.is_registered(run_id):
            for subscriber in lineage_ids:
                ctx.completion_registry.subscribe(run_id, subscriber)
        else:
            ctx.completion_registry.register(run_id, subscribers=lineage_ids)
    except Exception:
        logger.debug("Failed to register expansion completion event %s", run_id, exc_info=True)


async def _notify_completion(
    ctx: RegistryContext,
    run_id: str,
    result: dict[str, Any],
    *,
    message: str = "",
) -> None:
    """Notify completion subscribers when the registry is available."""
    if ctx.completion_registry is None:
        return
    try:
        await ctx.completion_registry.notify(run_id, result, message=message)
    except Exception:
        logger.debug("Failed to notify expansion completion for %s", run_id, exc_info=True)


def _resolve_current_session(ctx: RegistryContext) -> tuple[str, str] | dict[str, Any]:
    """Resolve the current MCP session to both caller ref and storage UUID."""
    session_ref = get_current_session_id()
    if not session_ref:
        return {"error": "No session context available. Ensure session_id is set."}
    try:
        resolved_session_id = ctx.resolve_session_id(session_ref)
    except (ValueError, LookupError) as e:
        return {"error": f"Cannot resolve session '{session_ref}': {e}"}
    return session_ref, resolved_session_id


def _get_execute_run_background() -> Any:
    from gobby.mcp_proxy.tools.tasks import _expansion

    return _expansion._execute_run_background


def create_expansion_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create the task expansion ops registry."""
    registry = InternalToolRegistry(
        name="gobby-tasks-expansion",
        description="Run-oriented task expansion operations",
    )

    _register_start_tool(registry, ctx)
    _register_reset_tool(registry, ctx)
    _register_read_tools(registry, ctx)
    _register_lifecycle_tools(registry, ctx)
    _register_validation_tools(registry, ctx)
    _register_qa_tools(registry, ctx)
    _register_plan_validation_tool(registry, ctx)
    return registry


def _register_start_tool(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    async def start_expansion_run(
        task_id: str,
        plan_file: str | None = None,
        auto_apply: bool = True,
        force_new: bool = False,
        reset_output: bool = False,
        provider: str | None = None,
        model: str | None = None,
        project: str | None = None,
        stage_pipeline_mode: bool | None = None,
    ) -> dict[str, Any]:
        session_result = _resolve_current_session(ctx)
        if isinstance(session_result, dict):
            return session_result
        _session_ref, resolved_session_id = session_result

        try:
            project_id = ctx.resolve_project_filter(project)
        except ValueError as e:
            return {"error": str(e)}

        try:
            resolved_task_id = resolve_task_id_for_mcp(ctx.task_manager, task_id, project_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Task not found: {e}"}

        result = start_expansion_run_impl(
            task_manager=ctx.task_manager,
            llm_service=ctx.llm_service,
            config=ctx.config,
            completion_registry=ctx.completion_registry,
            triggering_session_id=resolved_session_id,
            task_id=resolved_task_id,
            plan_file=plan_file,
            auto_apply=auto_apply,
            force_new=force_new,
            reset_output=reset_output,
            provider=provider,
            model=model,
            stage_pipeline_mode=stage_pipeline_mode,
        )
        if result.run_id is not None:
            _subscribe_completion(ctx, result.run_id, resolved_session_id)
        return result.to_dict()

    registry.register(
        name="start_expansion_run",
        description="Start a background expansion run for a task. Compiles a spec and optionally applies it.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task reference to expand"},
                "plan_file": {
                    "type": "string",
                    "description": "Optional plan file path relative to the project root",
                    "default": None,
                },
                "auto_apply": {
                    "type": "boolean",
                    "description": "When true, apply the compiled spec after compile succeeds",
                    "default": True,
                },
                "force_new": {
                    "type": "boolean",
                    "description": "When true, create a new run even if another run is active",
                    "default": False,
                },
                "reset_output": {
                    "type": "boolean",
                    "description": "Delete existing generated output before starting the run",
                    "default": False,
                },
                "provider": {
                    "type": "string",
                    "description": "Optional provider override",
                    "default": None,
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override",
                    "default": None,
                },
                "project": {
                    "type": "string",
                    "description": "Optional project ref for task resolution",
                    "default": None,
                },
                "stage_pipeline_mode": {
                    "type": ["boolean", "null"],
                    "description": (
                        "Internal: suppress parent expansion-stage transitions because "
                        "the owning stage pipeline terminal handler will advance the stage"
                    ),
                    "default": None,
                },
            },
            "required": ["task_id"],
        },
        func=start_expansion_run,
    )


def _register_reset_tool(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    async def reset_expansion_output(
        task_id: str,
        run_id: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        session_result = _resolve_current_session(ctx)
        if isinstance(session_result, dict):
            return session_result
        _session_ref, resolved_session_id = session_result

        try:
            project_id = ctx.resolve_project_filter(project)
        except ValueError as e:
            return {"error": str(e)}

        try:
            resolved_task_id = resolve_task_id_for_mcp(ctx.task_manager, task_id, project_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Task not found: {e}"}

        service = _build_expansion_service(ctx)
        try:
            result = service.reset_expansion_output(
                resolved_task_id,
                run_id=run_id,
                session_id=resolved_session_id,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        return {"success": True, "reset": result.to_dict()}

    registry.register(
        name="reset_expansion_output",
        description="Delete generated output for the latest or specified expansion run.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task reference to reset"},
                "run_id": {
                    "type": "string",
                    "description": "Optional expansion run ID to reset",
                    "default": None,
                },
                "project": {
                    "type": "string",
                    "description": "Optional project ref for task resolution",
                    "default": None,
                },
            },
            "required": ["task_id"],
        },
        func=reset_expansion_output,
    )


def _register_read_tools(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    async def get_expansion_run(run_id: str) -> dict[str, Any]:
        run_manager = LocalExpansionRunManager(ctx.task_manager.db)
        run = run_manager.get(run_id)
        if run is None:
            return {"error": f"Expansion run {run_id} not found"}
        return {"success": True, "run": _summarize_run(run)}

    registry.register(
        name="get_expansion_run",
        description="Get the current status and stored data for an expansion run.",
        input_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Expansion run ID"},
            },
            "required": ["run_id"],
        },
        func=get_expansion_run,
    )

    async def get_latest_expansion_run(
        task_id: str,
        project: str | None = None,
    ) -> dict[str, Any]:
        try:
            project_id = ctx.resolve_project_filter(project)
        except ValueError as e:
            return {"error": str(e)}

        try:
            resolved_task_id = resolve_task_id_for_mcp(ctx.task_manager, task_id, project_id)
        except (TaskNotFoundError, ValueError) as e:
            return {"error": f"Task not found: {e}"}

        run_manager = LocalExpansionRunManager(ctx.task_manager.db)
        run = run_manager.get_latest_for_task(resolved_task_id)
        if run is None:
            return {"success": True, "run": None}
        return {"success": True, "run": _summarize_run(run)}

    registry.register(
        name="get_latest_expansion_run",
        description="Get the most recent expansion run for a task.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task reference"},
                "project": {
                    "type": "string",
                    "description": "Optional project ref for task resolution",
                    "default": None,
                },
            },
            "required": ["task_id"],
        },
        func=get_latest_expansion_run,
    )


def _register_lifecycle_tools(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    async def resume_expansion_run(run_id: str) -> dict[str, Any]:
        session_result = _resolve_current_session(ctx)
        if isinstance(session_result, dict):
            return session_result
        _session_ref, resolved_session_id = session_result

        run_manager = LocalExpansionRunManager(ctx.task_manager.db)
        run = run_manager.get(run_id)
        if run is None:
            return {"error": f"Expansion run {run_id} not found"}

        current_task = _background_run_tasks.get(run_id)
        if current_task is not None and not current_task.done():
            _subscribe_completion(ctx, run_id, resolved_session_id)
            return {
                "success": True,
                "run_id": run_id,
                "status": run.status,
                "message": f"Expansion run {run_id} is already active.",
            }

        auto_apply = bool((run.options or {}).get("auto_apply", True))
        if run.status == "completed":
            return {
                "success": True,
                "run_id": run_id,
                "status": run.status,
                "message": f"Expansion run {run_id} is already completed.",
            }

        _subscribe_completion(ctx, run_id, resolved_session_id)
        execute_run_background = _get_execute_run_background()
        background_task = asyncio.create_task(
            execute_run_background(
                ctx,
                run_id,
                session_id=resolved_session_id,
                auto_apply=auto_apply,
            ),
            name=f"expansion-run-resume-{run_id}",
        )
        _register_background_task(run_id, background_task)
        return {"success": True, "run_id": run_id, "status": "running"}

    registry.register(
        name="resume_expansion_run",
        description="Resume a failed or interrupted expansion run.",
        input_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Expansion run ID"},
            },
            "required": ["run_id"],
        },
        func=resume_expansion_run,
    )

    async def cancel_expansion_run(run_id: str) -> dict[str, Any]:
        run_manager = LocalExpansionRunManager(ctx.task_manager.db)
        run = run_manager.get(run_id)
        if run is None:
            return {"error": f"Expansion run {run_id} not found"}
        if not run_manager.is_active_status(run.status):
            return {"success": True, "run": _summarize_run(run)}

        task = _background_run_tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        run = run_manager.cancel(run_id, error="Expansion run cancelled by user")
        if run is None:
            return {"error": f"Expansion run {run_id} not found"}
        if run.status != "cancelled":
            return {"success": True, "run": _summarize_run(run)}
        await _notify_completion(
            ctx,
            run_id,
            {"status": run.status, "run": _summarize_run(run)},
            message=f"Task expansion cancelled for {run.parent_task_id}.",
        )
        return {"success": True, "run": _summarize_run(run)}

    registry.register(
        name="cancel_expansion_run",
        description="Cancel an active expansion run.",
        input_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Expansion run ID"},
            },
            "required": ["run_id"],
        },
        func=cancel_expansion_run,
    )


def _register_validation_tools(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    async def validate_expansion_run(run_id: str) -> dict[str, Any]:
        service = _build_expansion_service(ctx)
        run_manager = LocalExpansionRunManager(ctx.task_manager.db)
        run = run_manager.get(run_id)
        if run is None:
            return {"error": f"Expansion run {run_id} not found"}
        if run.compiled_spec is None:
            return {"error": f"Expansion run {run_id} has no compiled spec"}

        compiled_validation = service.validate_compiled_spec(run.compiled_spec)
        applied_validation = None
        if run.task_id_map:
            applied_validation = service.validate_applied_run(run_id)
        return {
            "success": True,
            "run_id": run_id,
            "compiled": compiled_validation,
            "applied": applied_validation,
        }

    registry.register(
        name="validate_expansion_run",
        description="Validate a compiled or applied expansion run.",
        input_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Expansion run ID"},
            },
            "required": ["run_id"],
        },
        func=validate_expansion_run,
    )


def _register_qa_tools(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    async def save_expansion_qa_result(run_id: str, qa_result: dict[str, Any]) -> dict[str, Any]:
        run_manager = LocalExpansionRunManager(ctx.task_manager.db)
        run = run_manager.save_qa_result(run_id, qa_result)
        if run is None:
            return {"error": f"Expansion run {run_id} not found"}
        return {"success": True, "run": _summarize_run(run)}

    registry.register(
        name="save_expansion_qa_result",
        description="Persist QA findings on an expansion run.",
        input_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Expansion run ID"},
                "qa_result": {
                    "type": "object",
                    "description": "QA findings to store on the expansion run",
                },
            },
            "required": ["run_id", "qa_result"],
        },
        func=save_expansion_qa_result,
    )

    def run_expansion_qa_coverage(
        run_id: str,
        plan_path: str,
        plan_id: str,
        plan_hash: str,
        root_task: str,
        project_id: str,
        task_tree: str = "db",
        regenerate: bool = False,
    ) -> dict[str, Any]:
        run_manager = LocalExpansionRunManager(ctx.task_manager.db)
        run = run_manager.get(run_id)
        if run is None:
            return {"ok": False, "error": f"Expansion run {run_id} not found"}
        repo_path = ctx.get_project_repo_path(project_id or run.project_id)
        return run_qa_coverage(
            task_manager=ctx.task_manager,
            run=run,
            repo_path=repo_path,
            plan_path=plan_path,
            plan_id=plan_id,
            plan_hash=plan_hash,
            root_task_ref=root_task,
            project_id=project_id,
            task_tree=task_tree,
            regenerate=regenerate,
        )

    registry.register(
        name="run_expansion_qa_coverage",
        description=(
            "Run plan coverage for expansion QA with task-tree=db, persist the manifest, "
            "store task artifact pointers, and return the mechanical review action."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Expansion run ID"},
                "plan_path": {"type": "string", "description": "Plan file path"},
                "plan_id": {"type": "string", "description": "Stable plan identifier"},
                "plan_hash": {"type": "string", "description": "Expected SHA-256 plan hash"},
                "root_task": {"type": "string", "description": "Root task ref, e.g. #12725"},
                "project_id": {"type": "string", "description": "Project UUID"},
                "task_tree": {
                    "type": "string",
                    "description": "Coverage task tree source; only db is supported here",
                    "enum": ["db"],
                    "default": "db",
                },
                "regenerate": {
                    "type": "boolean",
                    "description": "Allow same-identity manifest regeneration",
                    "default": False,
                },
            },
            "required": [
                "run_id",
                "plan_path",
                "plan_id",
                "plan_hash",
                "root_task",
                "project_id",
            ],
        },
        func=run_expansion_qa_coverage,
    )

    async def check_expansion_qa_result(run_id: str) -> dict[str, Any]:
        run_manager = LocalExpansionRunManager(ctx.task_manager.db)
        run = run_manager.get(run_id)
        if run is None:
            return {"error": f"Expansion run {run_id} not found"}
        if run.qa_result is None:
            return {
                "success": True,
                "run_id": run_id,
                "qa_status": "skipped",
                "reason": "No QA result stored on expansion run",
            }
        return {
            "success": True,
            "run_id": run_id,
            "qa_status": "passed" if run.qa_result.get("passed") else "failed",
            "qa_result": run.qa_result,
        }

    registry.register(
        name="check_expansion_qa_result",
        description="Read QA findings stored on an expansion run.",
        input_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Expansion run ID"},
            },
            "required": ["run_id"],
        },
        func=check_expansion_qa_result,
    )


def _register_plan_validation_tool(registry: InternalToolRegistry, ctx: RegistryContext) -> None:
    async def validate_plan_file(plan_file: str) -> dict[str, Any]:
        service = _build_expansion_service(ctx)
        repo_path = None
        project_ctx = ctx.get_current_project_id()
        if project_ctx:
            repo_path_str = ctx.get_project_repo_path(project_ctx)
            repo_path = repo_path_str if repo_path_str else None
        plan_path = (
            Path(repo_path) / plan_file
            if repo_path and not Path(plan_file).is_absolute()
            else Path(plan_file)
        )
        return service.validate_plan_file(plan_path)

    registry.register(
        name="validate_plan_file",
        description="Validate a Plan-Coverage Contract plan file.",
        input_schema={
            "type": "object",
            "properties": {
                "plan_file": {"type": "string", "description": "Plan file path"},
            },
            "required": ["plan_file"],
        },
        func=validate_plan_file,
    )

"""Runtime orchestration for task expansion runs."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from gobby.mcp_proxy.tools._background_task_lifecycle import (
    register_background_task,
    resolve_background_loop,
)
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.expansion_runs import LocalExpansionRunManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.tasks.expansion_service import ExpansionService

logger = logging.getLogger(__name__)

__all__ = [
    "ExpansionRunResult",
    "_background_run_tasks",
    "_build_expansion_service",
    "_execute_run_background",
    "_register_background_task",
    "_summarize_run",
    "start_expansion_run_impl",
]

_background_run_tasks: dict[str, asyncio.Task[None]] = {}
_TERMINAL_EVENT_BY_STATUS = {
    "completed": "expansion_run_completed",
    "failed": "expansion_run_failed",
    "cancelled": "expansion_run_cancelled",
}


@dataclass(frozen=True)
class ExpansionRunResult:
    """Result from starting an expansion run."""

    success: bool
    run_id: str | None
    status: str
    reused: bool
    run: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "success": self.success,
            "run_id": self.run_id,
            "status": self.status,
            "reused": self.reused,
        }
        if self.run is not None:
            result["run"] = self.run
        if self.error is not None:
            result["error"] = self.error
        return result


def _register_background_task(run_id: str, task: asyncio.Task[None]) -> None:
    register_background_task(
        _background_run_tasks,
        run_id,
        task,
        logger=logger,
        description="Expansion background task",
    )


def _build_expansion_service(ctx: RegistryContext) -> ExpansionService:
    """Create an expansion service for the current registry context."""
    return ExpansionService(
        task_manager=ctx.task_manager,
        llm_service=ctx.llm_service,
        config=ctx.config,
        run_manager=LocalExpansionRunManager(ctx.task_manager.db),
    )


def _summarize_run(run: Any) -> dict[str, Any]:
    """Return a run dict with lightweight compiled-spec counts."""
    result: dict[str, Any] = run.to_dict()
    compiled_spec = run.compiled_spec or {}
    result["compiled_summary"] = {
        "phase_count": len(compiled_spec.get("phases") or []),
        "task_count": len(compiled_spec.get("tasks") or []),
        "dependency_count": len(compiled_spec.get("dependencies") or []),
    }
    return result


def _emit_terminal_event(
    completion_registry: Any | None,
    *,
    task_id: str,
    run_id: str,
    status: str,
    reason: str | None = None,
) -> None:
    event_name = _TERMINAL_EVENT_BY_STATUS.get(status)
    if event_name is None or completion_registry is None:
        return
    emit = getattr(completion_registry, "emit", None)
    if emit is None:
        return
    kwargs: dict[str, Any] = {"task_id": task_id, "run_id": run_id}
    if status == "failed" and reason is not None:
        kwargs["reason"] = reason
    emit(event_name, **kwargs)


def _is_stage_pipeline_expansion_run(task_manager: Any, task_id: str) -> bool:
    mutex = TaskDispatchMutexManager(task_manager.db).get_mutex(task_id)
    return mutex is not None and mutex.action_kind == "stage-pipeline:expansion"


async def _notify_completion_registry(
    completion_registry: Any | None,
    run_id: str,
    result: dict[str, Any],
    *,
    message: str,
) -> None:
    if completion_registry is None:
        return
    notify = getattr(completion_registry, "notify", None)
    if notify is None:
        return
    try:
        await notify(run_id, result, message=message)
    except Exception:
        logger.debug("Failed to notify expansion completion for %s", run_id, exc_info=True)


def _start_expansion_coroutine(coro: Any, run_id: str) -> Any:
    """Background the run on whichever loop is reachable, else run it inline.

    Returns the completed run when it had to run inline, and None once the run
    is scheduled. An internal MCP tool that awaits nothing is registered sync so
    the registry offloads it, which leaves no running loop here even though the
    daemon has one; resolve_background_loop recovers it from the context the
    registry set. Without that recovery this reported a finished run where the
    caller expects a started one (#20845).
    """
    loop = resolve_background_loop()
    if loop is None:
        return asyncio.run(coro)

    def _schedule() -> None:
        _register_background_task(
            run_id,
            loop.create_task(coro, name=f"expansion-run-{run_id}"),
        )

    try:
        on_target_loop = asyncio.get_running_loop() is loop
    except RuntimeError:
        on_target_loop = False
    if on_target_loop:
        _schedule()
    else:
        loop.call_soon_threadsafe(_schedule)
    return None


async def _execute_run_impl(
    *,
    task_manager: Any,
    llm_service: Any,
    config: Any,
    run_manager: LocalExpansionRunManager,
    completion_registry: Any | None,
    task_id: str,
    run_id: str,
    session_id: str | None,
    auto_apply: bool,
    stage_pipeline_mode: bool = False,
) -> Any:
    service = ExpansionService(
        task_manager=task_manager,
        llm_service=llm_service,
        config=config,
        run_manager=run_manager,
    )
    try:
        run = await service.compile_and_apply_run(
            run_id,
            session_id=session_id,
            auto_apply=auto_apply,
            suppress_parent_stage_transition=stage_pipeline_mode,
        )
        if not stage_pipeline_mode:
            _emit_terminal_event(
                completion_registry,
                task_id=task_id,
                run_id=run.id,
                status=run.status,
            )
        await _notify_completion_registry(
            completion_registry,
            run.id,
            {"status": run.status, "run": _summarize_run(run)},
            message=f"Task expansion completed for {run.parent_task_id}.",
        )
        return run
    except asyncio.CancelledError:
        cancelled_run = run_manager.cancel(run_id, error="Expansion run cancelled")
        if cancelled_run is not None:
            if not stage_pipeline_mode:
                _emit_terminal_event(
                    completion_registry,
                    task_id=task_id,
                    run_id=cancelled_run.id,
                    status=cancelled_run.status,
                )
            await _notify_completion_registry(
                completion_registry,
                cancelled_run.id,
                {"status": cancelled_run.status, "run": _summarize_run(cancelled_run)},
                message=f"Task expansion cancelled for {cancelled_run.parent_task_id}.",
            )
            return cancelled_run
        raise
    except Exception as e:
        failed_run = run_manager.fail(run_id, str(e))
        if failed_run is not None:
            if not stage_pipeline_mode:
                _emit_terminal_event(
                    completion_registry,
                    task_id=task_id,
                    run_id=failed_run.id,
                    status=failed_run.status,
                    reason=str(e),
                )
            await _notify_completion_registry(
                completion_registry,
                failed_run.id,
                {
                    "status": failed_run.status,
                    "error": str(e),
                    "run": _summarize_run(failed_run),
                },
                message=f"Task expansion failed for {failed_run.parent_task_id}.",
            )
            return failed_run
        raise


def start_expansion_run_impl(
    *,
    task_manager: Any,
    llm_service: Any,
    config: Any,
    completion_registry: Any | None,
    triggering_session_id: str | None,
    task_id: str,
    plan_file: str | None = None,
    auto_apply: bool = False,
    force_new: bool = False,
    provider: str | None = None,
    model: str | None = None,
    project: str | None = None,
    run_id: str | None = None,
    reset_output: bool = False,
    stage_pipeline_mode: bool | None = None,
) -> ExpansionRunResult:
    """Start an expansion run from MCP or in-process dispatcher code."""
    _ = project
    if task_manager is None:
        return ExpansionRunResult(False, run_id, "failed", False, error="task_manager is required")

    task = task_manager.get_task(task_id)
    if task is None:
        return ExpansionRunResult(False, run_id, "failed", False, error=f"Task {task_id} not found")

    run_manager = LocalExpansionRunManager(task_manager.db)
    service = ExpansionService(
        task_manager=task_manager,
        llm_service=llm_service,
        config=config,
        run_manager=run_manager,
    )
    if reset_output:
        try:
            service.reset_expansion_output(task.id, session_id=triggering_session_id)
        except ValueError as exc:
            return ExpansionRunResult(False, run_id, "failed", False, error=str(exc))
    existing_run = run_manager.get(run_id) if run_id is not None else None
    resolved_stage_pipeline_mode = (
        _is_stage_pipeline_expansion_run(task_manager, task.id)
        if stage_pipeline_mode is None
        else stage_pipeline_mode
    )
    if existing_run is not None:
        if not resolved_stage_pipeline_mode:
            _emit_terminal_event(
                completion_registry,
                task_id=task.id,
                run_id=existing_run.id,
                status=existing_run.status,
                reason=existing_run.error,
            )
        return ExpansionRunResult(
            True,
            existing_run.id,
            existing_run.status,
            True,
            run=_summarize_run(existing_run),
        )

    if not force_new:
        run_manager.cleanup_stale_runs(parent_task_id=task.id)
        active_run = run_manager.get_active_for_task(task.id)
        if active_run is not None:
            return ExpansionRunResult(
                True,
                active_run.id,
                active_run.status,
                True,
                run=_summarize_run(active_run),
            )

    run = run_manager.create(
        parent_task_id=task.id,
        project_id=task.project_id,
        triggering_session_id=triggering_session_id,
        input_source="plan" if plan_file else "task",
        plan_file=plan_file,
        provider=provider,
        model=model,
        options={"auto_apply": auto_apply},
        run_id=run_id,
    )
    coro = _execute_run_impl(
        task_manager=task_manager,
        llm_service=llm_service,
        config=config,
        run_manager=run_manager,
        completion_registry=completion_registry,
        task_id=task.id,
        run_id=run.id,
        session_id=triggering_session_id,
        auto_apply=auto_apply,
        stage_pipeline_mode=resolved_stage_pipeline_mode,
    )
    completed_run = _start_expansion_coroutine(coro, run.id)
    if completed_run is not None:
        return ExpansionRunResult(
            True,
            completed_run.id,
            completed_run.status,
            False,
            run=_summarize_run(completed_run),
        )

    return ExpansionRunResult(True, run.id, "running", False, run=_summarize_run(run))


async def _execute_run_background(
    ctx: RegistryContext,
    run_id: str,
    *,
    session_id: str,
    auto_apply: bool,
) -> None:
    """Compile and optionally apply an expansion run in the background."""
    run_manager = LocalExpansionRunManager(ctx.task_manager.db)
    run = run_manager.get(run_id)
    if run is None:
        return
    await _execute_run_impl(
        task_manager=ctx.task_manager,
        llm_service=ctx.llm_service,
        config=ctx.config,
        run_manager=run_manager,
        completion_registry=ctx.completion_registry,
        task_id=run.parent_task_id,
        run_id=run.id,
        session_id=session_id,
        auto_apply=auto_apply,
        stage_pipeline_mode=False,
    )

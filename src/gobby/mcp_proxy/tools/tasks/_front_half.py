"""Front-half conductor orchestration tools.

This module owns the requirements -> planning -> expansion -> test-architecture
state machine for the conductor's front half. It keeps task lifecycle semantics
strict by using labeled child tasks for stage ownership and returning a
machine-usable next action for pipelines to dispatch.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._expansion import (
    _build_expansion_service,
    _execute_run_background,
    _register_background_task,
    _resolve_current_session,
    _subscribe_completion,
    _summarize_run,
)
from gobby.mcp_proxy.tools.tasks._resolution import resolve_task_id_for_mcp
from gobby.storage.expansion_runs import ExpansionRun, LocalExpansionRunManager
from gobby.storage.tasks import Task, TaskNotFoundError
from gobby.tasks.state_semantics import get_claimed_session_id

FRONT_HALF_LABEL = "conductor:front-half"
FRONT_HALF_COMPLETE_LABEL = "conductor:front-half-complete"
STAGE_LABEL_PREFIX = "conductor-stage:"
PLANNING_ROUND_LABEL_PREFIX = "planning-round:"

NEEDS_REQUIREMENTS_PREFIX = "needs_requirements:"

_STAGE_LABELS = {
    "requirements": f"{STAGE_LABEL_PREFIX}requirements",
    "planning": f"{STAGE_LABEL_PREFIX}planning",
    "expansion": f"{STAGE_LABEL_PREFIX}expansion",
    "test_architecture": f"{STAGE_LABEL_PREFIX}test-architecture",
}


def create_front_half_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create the front-half conductor registry."""
    registry = InternalToolRegistry(
        name="gobby-tasks-front-half",
        description="Front-half conductor orchestration helpers",
    )

    async def front_half_tick(
        task_id: str,
        max_planning_rounds: int = 3,
        auto_dispatch_requirements: bool = False,
        expansion_provider: str | None = None,
        expansion_model: str | None = None,
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

        parent_task = ctx.task_manager.get_task(resolved_task_id)
        if not parent_task:
            return {"error": f"Task {task_id} not found"}

        _ensure_label(ctx, parent_task, FRONT_HALF_LABEL)

        artifacts = _artifact_paths(parent_task)

        requirements_task = _get_or_create_stage_task(
            ctx,
            parent_task,
            stage="requirements",
            description=_requirements_stage_description(parent_task),
        )

        if requirements_task.status not in ("review_approved", "closed"):
            stage_tasks = _stage_task_payload(requirements_task=requirements_task)
            if (
                auto_dispatch_requirements
                and requirements_task.status in ("open", "in_progress")
                and not get_claimed_session_id(requirements_task)
            ):
                return _response(
                    parent_task,
                    current_stage="requirements",
                    next_action="spawn_requirements_analyst",
                    message="Requirements stage is active and ready for analyst dispatch.",
                    artifacts=artifacts,
                    stage_tasks=stage_tasks,
                    dispatch=_dispatch_payload(
                        agent="requirements-analyst",
                        task=requirements_task,
                        prompt=_requirements_prompt(parent_task, requirements_task),
                    ),
                    front_half_complete=False,
                )
            return _response(
                parent_task,
                current_stage="requirements",
                next_action="wait_for_requirements_lock",
                message="Waiting for the requirements stage to be reviewed and locked.",
                artifacts=artifacts,
                stage_tasks=stage_tasks,
                front_half_complete=False,
            )

        requirements_task = _close_stage_task(
            ctx,
            requirements_task,
            reason="Requirements locked; planning stage unlocked.",
        )

        planning_task = _get_or_create_stage_task(
            ctx,
            parent_task,
            stage="planning",
            description=_planning_stage_description(parent_task, artifacts["plan_file"]),
            extra_labels=[f"{PLANNING_ROUND_LABEL_PREFIX}0"],
        )

        planning_round = _planning_round(planning_task)

        if planning_task.status == "escalated":
            escalation_reason = planning_task.escalation_reason or ""
            if escalation_reason.startswith(NEEDS_REQUIREMENTS_PREFIX):
                stage_tasks = _stage_task_payload(
                    requirements_task=requirements_task,
                    planning_task=planning_task,
                )
                return _response(
                    parent_task,
                    current_stage="planning",
                    next_action="wait_for_requirements_clarification",
                    message="Planner escalated back to requirements with explicit clarification needs.",
                    artifacts=artifacts,
                    stage_tasks=stage_tasks,
                    planning_round=planning_round,
                    max_planning_rounds=max_planning_rounds,
                    front_half_complete=False,
                )
            else:
                stage_tasks = _stage_task_payload(
                    requirements_task=requirements_task,
                    planning_task=planning_task,
                )
                return _response(
                    parent_task,
                    current_stage="planning",
                    next_action="front_half_failed",
                    message="Planning is escalated and requires human intervention.",
                    artifacts=artifacts,
                    stage_tasks=stage_tasks,
                    planning_round=planning_round,
                    max_planning_rounds=max_planning_rounds,
                    front_half_complete=False,
                )

        stage_tasks = _stage_task_payload(
            requirements_task=requirements_task,
            planning_task=planning_task,
        )
        if planning_task.status in ("open", "in_progress"):
            if planning_task.status == "open" and planning_round >= max_planning_rounds:
                return _response(
                    parent_task,
                    current_stage="planning",
                    next_action="front_half_failed",
                    message=(
                        "Planning failed to converge within the configured round budget. "
                        "Human escalation is required."
                    ),
                    artifacts=artifacts,
                    stage_tasks=stage_tasks,
                    planning_round=planning_round,
                    max_planning_rounds=max_planning_rounds,
                    front_half_complete=False,
                )
            if get_claimed_session_id(planning_task):
                return _response(
                    parent_task,
                    current_stage="planning",
                    next_action="wait_for_planner",
                    message="Planner is currently working on the plan artifact.",
                    artifacts=artifacts,
                    stage_tasks=stage_tasks,
                    planning_round=planning_round,
                    max_planning_rounds=max_planning_rounds,
                    front_half_complete=False,
                )
            return _response(
                parent_task,
                current_stage="planning",
                next_action="spawn_planner",
                message="Planning stage is ready for planner dispatch.",
                artifacts=artifacts,
                stage_tasks=stage_tasks,
                planning_round=planning_round,
                max_planning_rounds=max_planning_rounds,
                dispatch=_dispatch_payload(
                    agent="planner",
                    task=planning_task,
                    prompt=_planner_prompt(
                        parent_task,
                        planning_task,
                        plan_file=artifacts["plan_file"],
                        planning_round=planning_round,
                    ),
                ),
                front_half_complete=False,
            )

        if planning_task.status == "needs_review":
            if get_claimed_session_id(planning_task):
                return _response(
                    parent_task,
                    current_stage="planning",
                    next_action="wait_for_plan_adversary",
                    message="Adversary review is currently in progress.",
                    artifacts=artifacts,
                    stage_tasks=stage_tasks,
                    planning_round=planning_round,
                    max_planning_rounds=max_planning_rounds,
                    front_half_complete=False,
                )
            return _response(
                parent_task,
                current_stage="planning",
                next_action="spawn_plan_adversary",
                message="Planning stage is waiting for adversarial review.",
                artifacts=artifacts,
                stage_tasks=stage_tasks,
                planning_round=planning_round,
                max_planning_rounds=max_planning_rounds,
                dispatch=_dispatch_payload(
                    agent="plan-adversary",
                    task=planning_task,
                    prompt=_adversary_prompt(
                        parent_task,
                        planning_task,
                        plan_file=artifacts["plan_file"],
                        planning_round=planning_round,
                    ),
                ),
                front_half_complete=False,
            )

        if planning_task.status == "review_approved":
            planning_task = _close_stage_task(
                ctx,
                planning_task,
                reason="Plan approved; task expansion stage unlocked.",
            )
            stage_tasks["planning"] = _task_payload(planning_task)

        expansion_task = _get_or_create_stage_task(
            ctx,
            parent_task,
            stage="expansion",
            description=_expansion_stage_description(parent_task, artifacts["plan_file"]),
        )
        stage_tasks["expansion"] = _task_payload(expansion_task)

        expansion_result = await _advance_expansion_stage(
            ctx,
            parent_task=parent_task,
            expansion_task=expansion_task,
            plan_file=artifacts["plan_file"],
            provider=expansion_provider,
            model=expansion_model,
        )
        if expansion_result is not None:
            stage_tasks["expansion"] = _task_payload(expansion_result["expansion_task"])
            if expansion_result["next_action"] != "expansion_complete":
                return _response(
                    parent_task,
                    current_stage="expansion",
                    next_action=expansion_result["next_action"],
                    message=expansion_result["message"],
                    artifacts=artifacts,
                    stage_tasks=stage_tasks,
                    planning_round=planning_round,
                    max_planning_rounds=max_planning_rounds,
                    latest_expansion_run=expansion_result.get("latest_run"),
                    front_half_complete=False,
                )
            expansion_task = expansion_result["expansion_task"]

        test_architecture_task = _get_or_create_stage_task(
            ctx,
            parent_task,
            stage="test_architecture",
            description=_test_architecture_stage_description(
                parent_task,
                plan_file=artifacts["plan_file"],
                test_architecture_file=artifacts["test_architecture_file"],
            ),
        )
        stage_tasks["test_architecture"] = _task_payload(test_architecture_task)

        if test_architecture_task.status == "escalated":
            return _response(
                parent_task,
                current_stage="test_architecture",
                next_action="front_half_failed",
                message="Test architecture stage is escalated and requires human intervention.",
                artifacts=artifacts,
                stage_tasks=stage_tasks,
                planning_round=planning_round,
                max_planning_rounds=max_planning_rounds,
                front_half_complete=False,
            )

        if test_architecture_task.status in ("open", "in_progress"):
            if get_claimed_session_id(test_architecture_task):
                return _response(
                    parent_task,
                    current_stage="test_architecture",
                    next_action="wait_for_test_architect",
                    message="Test architect is currently drafting the test architecture artifact.",
                    artifacts=artifacts,
                    stage_tasks=stage_tasks,
                    planning_round=planning_round,
                    max_planning_rounds=max_planning_rounds,
                    front_half_complete=False,
                )
            return _response(
                parent_task,
                current_stage="test_architecture",
                next_action="spawn_test_architect",
                message="Test architecture stage is ready for dispatch.",
                artifacts=artifacts,
                stage_tasks=stage_tasks,
                planning_round=planning_round,
                max_planning_rounds=max_planning_rounds,
                dispatch=_dispatch_payload(
                    agent="test-architect",
                    task=test_architecture_task,
                    prompt=_test_architect_prompt(
                        parent_task,
                        test_architecture_task,
                        plan_file=artifacts["plan_file"],
                        test_architecture_file=artifacts["test_architecture_file"],
                    ),
                ),
                front_half_complete=False,
            )

        if test_architecture_task.status == "needs_review":
            return _response(
                parent_task,
                current_stage="test_architecture",
                next_action="wait_for_test_architecture_review",
                message="Waiting for the test architecture artifact to be reviewed and approved.",
                artifacts=artifacts,
                stage_tasks=stage_tasks,
                planning_round=planning_round,
                max_planning_rounds=max_planning_rounds,
                front_half_complete=False,
            )

        if test_architecture_task.status == "review_approved":
            test_architecture_task = _close_stage_task(
                ctx,
                test_architecture_task,
                reason="Test architecture approved; front-half delivery stages complete.",
            )
            stage_tasks["test_architecture"] = _task_payload(test_architecture_task)

        _ensure_label(ctx, parent_task, FRONT_HALF_COMPLETE_LABEL)
        parent_task = ctx.task_manager.get_task(parent_task.id)
        return _response(
            parent_task,
            current_stage="complete",
            next_action="front_half_complete",
            message="Front-half stages are complete. Delivery can advance to development orchestration.",
            artifacts=artifacts,
            stage_tasks=stage_tasks,
            planning_round=planning_round,
            max_planning_rounds=max_planning_rounds,
            front_half_complete=True,
        )

    registry.register(
        name="front_half_tick",
        description=(
            "Advance the front-half conductor state machine for a parent task. "
            "Ensures stage tasks exist, starts expansion runs when appropriate, "
            "and returns the next action for a dispatcher pipeline."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Parent task reference (#N, path, or UUID).",
                },
                "max_planning_rounds": {
                    "type": "integer",
                    "description": "Maximum planner/adversary revision rounds before failure.",
                    "default": 3,
                },
                "auto_dispatch_requirements": {
                    "type": "boolean",
                    "description": (
                        "When true, return a requirements-analyst dispatch action while "
                        "requirements are still open."
                    ),
                    "default": False,
                },
                "expansion_provider": {
                    "type": "string",
                    "description": "Optional provider override for the expansion run.",
                    "default": None,
                },
                "expansion_model": {
                    "type": "string",
                    "description": "Optional model override for the expansion run.",
                    "default": None,
                },
                "project": {
                    "type": "string",
                    "description": "Optional project ref for task resolution.",
                    "default": None,
                },
            },
            "required": ["task_id"],
        },
        func=front_half_tick,
    )

    return registry


def _stage_task_payload(
    *,
    requirements_task: Task | None = None,
    planning_task: Task | None = None,
    expansion_task: Task | None = None,
    test_architecture_task: Task | None = None,
) -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    if requirements_task is not None:
        payload["requirements"] = _task_payload(requirements_task)
    if planning_task is not None:
        payload["planning"] = _task_payload(planning_task)
    if expansion_task is not None:
        payload["expansion"] = _task_payload(expansion_task)
    if test_architecture_task is not None:
        payload["test_architecture"] = _task_payload(test_architecture_task)
    return payload


def _response(
    parent_task: Task,
    *,
    current_stage: str,
    next_action: str,
    message: str,
    artifacts: dict[str, str],
    stage_tasks: dict[str, dict[str, Any]],
    front_half_complete: bool,
    dispatch: dict[str, Any] | None = None,
    planning_round: int | None = None,
    max_planning_rounds: int | None = None,
    latest_expansion_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "success": True,
        "task_id": parent_task.id,
        "task_ref": _task_ref(parent_task),
        "current_stage": current_stage,
        "next_action": next_action,
        "message": message,
        "front_half_complete": front_half_complete,
        "artifacts": artifacts,
        "stage_tasks": stage_tasks,
    }
    if dispatch is not None:
        response["dispatch"] = dispatch
    if planning_round is not None:
        response["planning_round"] = planning_round
    if max_planning_rounds is not None:
        response["max_planning_rounds"] = max_planning_rounds
    if latest_expansion_run is not None:
        response["latest_expansion_run"] = latest_expansion_run
    return response


def _artifact_paths(parent_task: Task) -> dict[str, str]:
    ident = str(parent_task.seq_num) if parent_task.seq_num is not None else parent_task.id[:8]
    return {
        "plan_file": f".gobby/plans/task-{ident}-plan.md",
        "test_architecture_file": f".gobby/test-architecture/task-{ident}-test-architecture.md",
    }


def _task_ref(task: Task) -> str:
    return f"#{task.seq_num}" if task.seq_num is not None else task.id


def _task_payload(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "ref": _task_ref(task),
        "title": task.title,
        "status": task.status,
        "claimed": bool(get_claimed_session_id(task)),
        "labels": list(task.labels or []),
        "escalation_reason": task.escalation_reason,
    }


def _dispatch_payload(agent: str, task: Task, prompt: str) -> dict[str, Any]:
    return {
        "agent": agent,
        "task_id": task.id,
        "task_ref": _task_ref(task),
        "prompt": prompt,
    }


def _has_label(task: Task, label: str) -> bool:
    return label in (task.labels or [])


def _ensure_label(ctx: RegistryContext, task: Task, label: str) -> Task:
    if _has_label(task, label):
        return task
    return ctx.task_manager.add_label(task.id, label)


def _close_stage_task(ctx: RegistryContext, task: Task, *, reason: str) -> Task:
    if task.status == "closed":
        return task
    return ctx.task_manager.close_task(task.id, reason=reason)


def _planning_round(task: Task) -> int:
    for label in task.labels or []:
        if label.startswith(PLANNING_ROUND_LABEL_PREFIX):
            suffix = label.removeprefix(PLANNING_ROUND_LABEL_PREFIX)
            try:
                return int(suffix)
            except ValueError:
                return 0
    return 0


def _set_planning_round(ctx: RegistryContext, task: Task, planning_round: int) -> Task:
    labels = [
        label for label in (task.labels or []) if not label.startswith(PLANNING_ROUND_LABEL_PREFIX)
    ]
    labels.append(f"{PLANNING_ROUND_LABEL_PREFIX}{planning_round}")
    return ctx.task_manager.update_task(task.id, labels=labels)


def _get_or_create_stage_task(
    ctx: RegistryContext,
    parent_task: Task,
    *,
    stage: str,
    description: str,
    extra_labels: list[str] | None = None,
) -> Task:
    existing = _find_stage_task(ctx, parent_task.id, stage=stage)
    if existing is not None:
        return existing

    labels = [FRONT_HALF_LABEL, _STAGE_LABELS[stage]]
    if extra_labels:
        labels.extend(extra_labels)
    created = ctx.task_manager.create_task(
        project_id=parent_task.project_id,
        title=f"{_stage_title(stage)} for {_task_ref(parent_task)}",
        description=description,
        parent_task_id=parent_task.id,
        task_type="task",
        category="planning",
        labels=labels,
    )
    return created


def _find_stage_task(ctx: RegistryContext, parent_task_id: str, *, stage: str) -> Task | None:
    tasks = ctx.task_manager.list_tasks(
        parent_task_id=parent_task_id,
        label=_STAGE_LABELS[stage],
        limit=20,
        sort_by="updated_at",
        sort_order="desc",
    )
    if not tasks:
        return None
    for task in tasks:
        if task.status != "closed":
            return task
    return tasks[0]


def _stage_title(stage: str) -> str:
    return {
        "requirements": "Requirements lock",
        "planning": "Implementation plan",
        "expansion": "Task expansion",
        "test_architecture": "Test architecture",
    }[stage]


def _requirements_stage_description(parent_task: Task) -> str:
    return (
        f"Front-half requirements gate for parent task {_task_ref(parent_task)}.\n\n"
        "The parent task remains the canonical requirements artifact. "
        "Use this stage task to track when requirements are clear enough to lock "
        "for downstream planning."
    )


def _planning_stage_description(parent_task: Task, plan_file: str) -> str:
    return (
        f"Front-half planning stage for parent task {_task_ref(parent_task)}.\n\n"
        f"Plan artifact: {plan_file}\n"
        f"Parent task: {_task_ref(parent_task)}\n\n"
        f"If requirements are insufficient, escalate with a reason starting "
        f"'{NEEDS_REQUIREMENTS_PREFIX}'.\n"
        "If adversarial review requests changes, the review agent calls "
        "`mark_task_review_rejected` with the next planning round and returns "
        "the task to open."
    )


def _expansion_stage_description(parent_task: Task, plan_file: str) -> str:
    return (
        f"Front-half expansion stage for parent task {_task_ref(parent_task)}.\n\n"
        f"Approved plan artifact: {plan_file}\n\n"
        "This stage compiles and applies the approved plan into executable child tasks."
    )


def _test_architecture_stage_description(
    parent_task: Task,
    *,
    plan_file: str,
    test_architecture_file: str,
) -> str:
    return (
        f"Front-half test architecture stage for parent task {_task_ref(parent_task)}.\n\n"
        f"Plan artifact: {plan_file}\n"
        f"Test architecture artifact: {test_architecture_file}\n\n"
        "Draft the coverage strategy only after the approved plan has been expanded "
        "into executable child tasks."
    )


async def _advance_expansion_stage(
    ctx: RegistryContext,
    *,
    parent_task: Task,
    expansion_task: Task,
    plan_file: str,
    provider: str | None,
    model: str | None,
) -> dict[str, Any] | None:
    if expansion_task.status == "closed":
        return {
            "next_action": "expansion_complete",
            "message": "",
            "expansion_task": expansion_task,
        }

    if expansion_task.status == "escalated":
        return {
            "next_action": "front_half_failed",
            "message": "Expansion stage is escalated and requires human intervention.",
            "expansion_task": expansion_task,
        }

    repo_path_str = ctx.get_project_repo_path(parent_task.project_id)
    repo_path = Path(repo_path_str) if repo_path_str else None
    plan_path = repo_path / plan_file if repo_path is not None else Path(plan_file)
    if not plan_path.exists():
        expansion_task = ctx.task_manager.escalate_task(
            expansion_task.id,
            reason=f"missing_plan_artifact: approved plan file not found at {plan_file}",
        )
        return {
            "next_action": "front_half_failed",
            "message": f"Expansion cannot start because the approved plan file is missing: {plan_file}",
            "expansion_task": expansion_task,
        }

    run_manager = LocalExpansionRunManager(ctx.task_manager.db)
    active_run = run_manager.get_active_for_task(parent_task.id)
    if active_run is not None:
        return {
            "next_action": "wait_for_expansion",
            "message": "Expansion run is still active.",
            "expansion_task": expansion_task,
            "latest_run": _summarize_run(active_run),
        }

    latest_run = run_manager.get_latest_for_task(parent_task.id)
    if latest_run is None:
        run_summary = await _start_expansion_run(
            ctx,
            parent_task=parent_task,
            plan_file=plan_file,
            provider=provider,
            model=model,
        )
        if run_summary.get("error"):
            expansion_task = ctx.task_manager.escalate_task(
                expansion_task.id,
                reason=f"expansion_start_failed: {run_summary['error']}",
            )
            return {
                "next_action": "front_half_failed",
                "message": run_summary["error"],
                "expansion_task": expansion_task,
            }
        return {
            "next_action": "wait_for_expansion",
            "message": "Expansion run started.",
            "expansion_task": expansion_task,
            "latest_run": run_summary,
        }

    if latest_run.status in {"pending", "running", "compiled", "applying"}:
        return {
            "next_action": "wait_for_expansion",
            "message": "Expansion run is still active.",
            "expansion_task": expansion_task,
            "latest_run": _summarize_run(latest_run),
        }

    if latest_run.status != "completed":
        expansion_task = ctx.task_manager.escalate_task(
            expansion_task.id,
            reason=f"expansion_run_failed: {latest_run.error or latest_run.status}",
        )
        return {
            "next_action": "front_half_failed",
            "message": "Expansion run did not complete successfully.",
            "expansion_task": expansion_task,
            "latest_run": _summarize_run(latest_run),
        }

    validation_error = _validate_completed_expansion_run(ctx, latest_run)
    if validation_error is not None:
        expansion_task = ctx.task_manager.escalate_task(
            expansion_task.id,
            reason=f"expansion_validation_failed: {validation_error}",
        )
        return {
            "next_action": "front_half_failed",
            "message": validation_error,
            "expansion_task": expansion_task,
            "latest_run": _summarize_run(latest_run),
        }

    expansion_task = _close_stage_task(
        ctx,
        expansion_task,
        reason="Expansion run completed successfully and produced executable child tasks.",
    )
    return {
        "next_action": "expansion_complete",
        "message": "Expansion completed successfully.",
        "expansion_task": expansion_task,
        "latest_run": _summarize_run(latest_run),
    }


def _validate_completed_expansion_run(ctx: RegistryContext, run: ExpansionRun) -> str | None:
    if not run.created_task_ids:
        return "Expansion run completed but did not create any child tasks."
    if run.compiled_spec is None:
        return "Expansion run completed without a compiled spec."

    service = _build_expansion_service(ctx)
    compiled_validation = service.validate_compiled_spec(run.compiled_spec)
    if not compiled_validation.get("valid", False):
        return f"Compiled expansion spec is invalid: {compiled_validation}"

    applied_validation = service.validate_applied_run(run.id)
    if not applied_validation.get("valid", False):
        return f"Applied expansion run is invalid: {applied_validation}"

    return None


async def _start_expansion_run(
    ctx: RegistryContext,
    *,
    parent_task: Task,
    plan_file: str,
    provider: str | None,
    model: str | None,
) -> dict[str, Any]:
    session_result = _resolve_current_session(ctx)
    if isinstance(session_result, dict):
        return session_result

    _session_ref, resolved_session_id = session_result
    run_manager = LocalExpansionRunManager(ctx.task_manager.db)
    active_run = run_manager.get_active_for_task(parent_task.id)
    if active_run is not None:
        _subscribe_completion(ctx, active_run.id, resolved_session_id)
        return _summarize_run(active_run)

    run = run_manager.create(
        parent_task_id=parent_task.id,
        project_id=parent_task.project_id,
        triggering_session_id=resolved_session_id,
        input_source="plan",
        plan_file=plan_file,
        provider=provider,
        model=model,
        options={"auto_apply": True},
    )
    _subscribe_completion(ctx, run.id, resolved_session_id)

    background_task = asyncio.create_task(
        _execute_run_background(
            ctx,
            run.id,
            session_id=resolved_session_id,
            auto_apply=True,
        ),
        name=f"front-half-expansion-{run.id}",
    )
    _register_background_task(run.id, background_task)
    return _summarize_run(run)


def _requirements_prompt(parent_task: Task, requirements_task: Task) -> str:
    return (
        f"Work the requirements stage task {_task_ref(requirements_task)} for parent task "
        f"{_task_ref(parent_task)}: {parent_task.title}.\n\n"
        "Collaborate with the human to clarify scope, constraints, assumptions, and "
        "acceptance criteria. The parent task record is the canonical requirements "
        "artifact.\n\n"
        "When the requirements are ready for human review, call mark_task_needs_review "
        "on the assigned stage task. If requirements are still missing, escalate the "
        "stage task with concrete questions."
    )


def _planner_prompt(
    parent_task: Task,
    planning_task: Task,
    *,
    plan_file: str,
    planning_round: int,
) -> str:
    return (
        f"Draft or revise the implementation plan for parent task {_task_ref(parent_task)}: "
        f"{parent_task.title}.\n\n"
        f"Assigned planning task: {_task_ref(planning_task)}\n"
        f"Plan artifact path: {plan_file}\n"
        f"Planning round: {planning_round + 1}\n\n"
        "Use the parent task as the canonical requirements artifact. The parent task "
        "description may reference supporting plan/spec docs; read them when relevant. "
        "If this is a revision round, review the planning task description for adversary "
        "findings before updating the plan.\n\n"
        f"If requirements are insufficient, escalate the assigned planning task with a "
        f"reason starting '{NEEDS_REQUIREMENTS_PREFIX}' and include the concrete missing "
        "questions. Otherwise, write or update the plan artifact and call "
        "mark_task_needs_review on the planning task when it is ready for adversarial review."
    )


def _adversary_prompt(
    parent_task: Task, planning_task: Task, *, plan_file: str, planning_round: int
) -> str:
    return (
        f"Adversarially review the plan for parent task {_task_ref(parent_task)}: "
        f"{parent_task.title}.\n\n"
        f"Assigned planning task: {_task_ref(planning_task)}\n"
        f"Plan artifact path: {plan_file}\n\n"
        f"Display round: {planning_round + 1}\n\n"
        "Focus on missing requirements, bad sequencing, unhandled risks, weak testability, "
        "and gaps between the task and the plan. Do not manufacture findings if the plan "
        "is sound.\n\n"
        "If blocking issues remain, append a concise 'Adversary Findings' section to the "
        "planning task description with structured findings, then call "
        f"mark_task_review_rejected(..., round={planning_round + 1}). "
        "If the plan is sound, approve the planning task with mark_task_review_approved."
    )


def _test_architect_prompt(
    parent_task: Task,
    test_architecture_task: Task,
    *,
    plan_file: str,
    test_architecture_file: str,
) -> str:
    return (
        f"Draft the test architecture for parent task {_task_ref(parent_task)}: "
        f"{parent_task.title}.\n\n"
        f"Assigned test architecture task: {_task_ref(test_architecture_task)}\n"
        f"Approved plan artifact: {plan_file}\n"
        f"Test architecture artifact path: {test_architecture_file}\n\n"
        "Use the approved plan and the expanded child tasks to define coverage, risks, "
        "and test strategy. Write the artifact, then call mark_task_needs_review when "
        "it is ready for approval. If the required context is missing, escalate the stage task."
    )

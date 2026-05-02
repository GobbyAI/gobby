"""Pre-flight plan validation gate for agent spawns.

When an agent like ``planner`` or ``plan-adversary`` is spawned against a task
that carries a ``plan_file_path`` artifact, the spawn pipeline validates the
plan against the Plan-Coverage Contract before letting the runner start. This
catches structural drift the parser silently drops (phase headings whose IDs
miss ``^P\\d+$``, missing ``kind:`` annotations, malformed deferral objects)
before wasting an LLM call on a structurally broken plan.

The gate is a no-op for non-planning agents and for tasks with no plan
artifact attached. It is a strict gate when it does fire — a structured
``PlanValidationError`` payload short-circuits the spawn.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Agent names whose spawn must be gated by plan validation. Centralized so the
# spawn pipeline and tests share one source of truth.
PLANNING_AGENTS = frozenset({"planner", "plan-adversary"})


def validate_plan_for_agent_spawn(
    agent_name: str | None,
    task_id: str | None,
    task_manager: Any | None,
    code_index: Any | None = None,
) -> dict[str, Any] | None:
    """Validate a planning agent's plan artifact before spawn.

    Args:
        agent_name: The agent definition name being spawned. Only ``planner``
            and ``plan-adversary`` trigger the gate; other agents pass through.
        task_id: The task the agent will work against. Without a task there is
            nothing to look up a plan artifact from, so the gate no-ops.
        task_manager: ``LocalTaskManager`` used to fetch task artifacts.

    Returns:
        ``None`` when the gate does not apply or the plan validates clean.
        A structured ``{"success": False, "error": "PlanValidationError: ..."}``
        dict when the gate fires and the plan fails validation.
    """
    if agent_name not in PLANNING_AGENTS:
        return None
    if not task_id or task_manager is None:
        return None

    artifacts = _safe_get_artifacts(task_manager, task_id)
    if artifacts is None or not artifacts.plan_file_path:
        # No plan artifact recorded — caller embeds the plan in the prompt.
        # Skill-level pre-flight (Step 5.5 in /gobby plan) handles those cases;
        # this gate only fires when there's a concrete file path to validate.
        return None

    plan_path = Path(artifacts.plan_file_path)
    if not plan_path.is_absolute():
        plan_path = Path.cwd() / plan_path

    from gobby.plans.consumer_sweep import run_consumer_sweep
    from gobby.plans.parser import PlanParseError, parse_plan
    from gobby.tasks.expansion._compile import validate_plan_file

    result = validate_plan_file(None, plan_path)
    if result.get("valid"):
        task = _safe_get_task(task_manager, task_id)
        project_id = _task_project_id(task)
        try:
            plan_doc = parse_plan(plan_path, parse_mode="draft")
        except (OSError, PlanParseError):
            return None
        sweep = run_consumer_sweep(plan_doc, project_id=project_id, code_index=code_index)
        if sweep.valid:
            return None
        result = {
            "valid": False,
            "errors": sweep.errors,
            "consumer_sweep": sweep.to_dict(),
        }

    errors = result.get("errors", [])
    error_summary = "; ".join(errors) if errors else "Plan validation failed"
    logger.warning(
        "Refusing %s spawn for task %s: PlanValidationError: %s",
        agent_name,
        task_id,
        error_summary,
    )
    payload = {
        "success": False,
        "error": f"PlanValidationError: {error_summary}",
        "plan_file_path": str(plan_path),
        "validator_errors": list(errors),
    }
    if "semantic_lint" in result:
        payload["semantic_lint"] = result["semantic_lint"]
    if "consumer_sweep" in result:
        payload["consumer_sweep"] = result["consumer_sweep"]
    return payload


def _safe_get_artifacts(task_manager: Any, task_id: str) -> Any | None:
    getter = getattr(task_manager, "get_artifacts", None)
    if getter is None:
        return None
    try:
        return getter(task_id)
    except Exception as exc:
        logger.debug("Failed to load task artifacts for %s: %s", task_id, exc)
        return None


def _safe_get_task(task_manager: Any, task_id: str) -> Any | None:
    getter = getattr(task_manager, "get_task", None)
    if getter is None:
        return None
    try:
        return getter(task_id)
    except Exception as exc:
        logger.debug("Failed to load task %s for plan validation gate: %s", task_id, exc)
        return None


def _task_project_id(task: Any | None) -> str | None:
    if task is None:
        return None
    if isinstance(task, dict):
        value = task.get("project_id")
    else:
        value = getattr(task, "project_id", None)
    return str(value) if value else None

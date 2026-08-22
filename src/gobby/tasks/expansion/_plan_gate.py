"""Pre-flight plan validation gate for agent spawns.

When an agent like ``planner`` or ``plan-adversary`` is spawned against a task
that carries a ``plan_file_path`` artifact, the spawn pipeline validates the
plan against the Plan-Coverage Contract before letting the runner start. This
catches structural drift the parser silently drops (phase headings whose IDs
miss ``^P\\d+$``, missing ``kind:`` annotations, malformed deferral objects)
before wasting an LLM call on a structurally broken plan.

The gate is a no-op for non-planning agents and for tasks with no plan
artifact attached. Structural failures block every planning role. Symbol-only
failures let authoring roles start with repair diagnostics while adversary
review remains blocked.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import psycopg

from gobby.utils.project_context import get_project_context

logger = logging.getLogger(__name__)

# Agent names whose spawn must be gated by plan validation. Centralized so the
# spawn pipeline and tests share one source of truth.
PLANNING_AGENTS = frozenset({"planner", "plan-adversary", "plan-enhancer"})
PLAN_REPAIR_AGENTS = frozenset({"planner", "plan-enhancer"})


def validate_plan_for_agent_spawn(
    agent_name: str | None,
    task_id: str | None,
    task_manager: Any | None,
    code_index: Any | None = None,
) -> dict[str, Any] | None:
    """Validate a recorded plan, skipping the gate on transient database errors."""
    try:
        return _validate_plan_for_agent_spawn(
            agent_name,
            task_id,
            task_manager,
            code_index,
        )
    except psycopg.Error as exc:
        logger.warning(
            "Skipping plan validation gate for %s spawn on task %s because database "
            "access failed: %s",
            agent_name,
            task_id,
            exc,
        )
        return None


def _validate_plan_for_agent_spawn(
    agent_name: str | None,
    task_id: str | None,
    task_manager: Any | None,
    code_index: Any | None = None,
) -> dict[str, Any] | None:
    """Validate a planning agent's plan artifact before spawn.

    Args:
        agent_name: The agent definition name being spawned. Only ``planner``,
            ``plan-adversary``, and ``plan-enhancer`` trigger the gate; other
            agents pass through.
        task_id: The task the agent will work against. Without a task there is
            nothing to look up a plan artifact from, so the gate no-ops.
        task_manager: ``LocalTaskManager`` used to fetch task artifacts.

    Returns:
        ``None`` when the gate does not apply or the plan validates clean.
        A structured failure when validation blocks the role, or a successful
        prompt-append payload when an authoring role may repair symbol Targets.
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

    from gobby.tasks.expansion._validate import validate_plan_file

    project_context = get_project_context(plan_path.parent)
    task = task_manager.get_task(task_id)
    expected_project_id = getattr(task, "project_id", None)
    result = validate_plan_file(
        None,
        plan_path,
        project_context=project_context,
        expected_project_id=(expected_project_id if isinstance(expected_project_id, str) else None),
        code_index=code_index,
        require_symbol_validation=True,
    )
    if result.get("valid"):
        return None

    errors = result.get("errors", [])
    warnings = result.get("warnings", [])
    error_summary = "; ".join(errors) if errors else "Plan validation failed"
    symbol_validation = result.get("symbol_validation")
    if (
        agent_name in PLAN_REPAIR_AGENTS
        and isinstance(symbol_validation, dict)
        and symbol_validation.get("status") == "failed"
    ):
        logger.warning(
            "Starting %s for task %s with symbol target diagnostics: %s",
            agent_name,
            task_id,
            error_summary,
        )
        return {
            "success": True,
            "plan_file_path": str(plan_path),
            "symbol_validation": symbol_validation,
            "prompt_append": _symbol_repair_prompt(symbol_validation),
        }

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
    if warnings:
        payload["validator_warnings"] = list(warnings)
    if "semantic_lint" in result:
        payload["semantic_lint"] = result["semantic_lint"]
    if symbol_validation is not None:
        payload["symbol_validation"] = symbol_validation
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


def _symbol_repair_prompt(symbol_validation: dict[str, Any]) -> str:
    issues = symbol_validation.get("issues")
    diagnostics: list[str] = []
    if isinstance(issues, list):
        for issue in issues:
            if isinstance(issue, dict):
                diagnostics.append(
                    f"- [{issue.get('code', 'symbol_validation')}] "
                    f"{issue.get('message', 'Symbol target validation failed')}"
                )
    details = "\n".join(diagnostics) or "- Symbol target validation failed"
    return (
        "Repair the plan's Targets blocks before completing this planning pass. "
        "Use exact gcode qualified_name references, or a justified `::*` scope.\n"
        f"Symbol validation diagnostics:\n{details}"
    )

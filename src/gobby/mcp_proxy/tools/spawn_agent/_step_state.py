"""Spawn-time step-instance persistence and initial step state."""

from __future__ import annotations

import logging
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.agent_models import AgentDefinitionBody, AgentStepWorkflowBody
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance

logger = logging.getLogger(__name__)


def _require_step_workflow(agent_body: AgentDefinitionBody) -> AgentStepWorkflowBody:
    snapshot = agent_body.step_workflow
    if snapshot is None:
        raise ValueError(
            f"Cannot initialize step state for an agent with no steps: {agent_body.name}"
        )
    return snapshot


def _transition_condition_met(condition: str | None, variables: dict[str, Any]) -> bool:
    if not condition:
        return True
    try:
        evaluator = SafeExpressionEvaluator(
            context={"vars": variables, "variables": variables},
            allowed_funcs={
                "len": len,
                "bool": bool,
                "str": str,
                "int": int,
                "list": list,
                "dict": dict,
                "any": any,
                "all": all,
            },
        )
        return evaluator.evaluate(condition)
    except ValueError as exc:
        logger.warning("Failed to evaluate initial step transition %r: %s", condition, exc)
        return False


def _advance_initial_step(
    snapshot: AgentStepWorkflowBody,
    current_step: str,
    variables: dict[str, Any],
    *,
    agent_name: str,
) -> str:
    steps = {step.name: step for step in snapshot.steps}
    max_transitions = len(steps) + 1

    for _ in range(max_transitions):
        step = steps.get(current_step)
        if step is None:
            return current_step

        for transition in step.transitions:
            if not _transition_condition_met(transition.when, variables):
                continue
            if transition.to not in steps:
                logger.warning(
                    "Initial step transition to unknown step %r in agent %r",
                    transition.to,
                    agent_name,
                )
                continue
            current_step = transition.to
            break
        else:
            return current_step

    logger.warning(
        "Stopped initial step transition chain for agent %r after %d transitions",
        agent_name,
        max_transitions,
    )
    return current_step


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def initial_step_state_for_spawn(
    snapshot: AgentStepWorkflowBody,
    *,
    agent_name: str,
    task_owned_by_child: bool,
    initial_variables: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return the initial step workflow state for a spawned agent."""
    step_variables = dict(snapshot.variables)
    if initial_variables and "additional_skills" in initial_variables:
        step_variables["additional_skills"] = initial_variables["additional_skills"]

    additional_skills = _normalize_string_list(step_variables.get("additional_skills"))
    step_variables["additional_skills"] = additional_skills
    step_variables["additional_skills_loaded"] = not additional_skills or all(
        skill in _normalize_string_list(step_variables.get("loaded_skills"))
        for skill in additional_skills
    )

    if not snapshot.steps:
        raise ValueError("Cannot initialize step state for an agent with no steps")
    first_step = snapshot.steps[0]
    current_step = first_step.name

    if task_owned_by_child and first_step.name == "claim":
        step_variables["task_claimed"] = True

    current_step = _advance_initial_step(
        snapshot,
        current_step,
        step_variables,
        agent_name=agent_name,
    )
    return current_step, step_variables


def persist_initial_step_instance(
    db: HubDatabase,
    agent_body: AgentDefinitionBody,
    *,
    session_id: str,
    step_workflow_id: str | None,
    initial_variables: dict[str, Any] | None = None,
    task_owned_by_child: bool = False,
) -> None:
    """Persist the unclaimed (or claimed) initial snapshot for a stepful spawn."""
    snapshot = _require_step_workflow(agent_body)
    current_step, variables = initial_step_state_for_spawn(
        snapshot,
        agent_name=agent_body.name,
        task_owned_by_child=task_owned_by_child,
        initial_variables=initial_variables,
    )
    instance = build_step_instance(
        agent_body,
        session_id=session_id,
        step_workflow_id=step_workflow_id,
        variables=variables,
        current_step=current_step,
    )
    AgentStepInstanceManager(db).save(instance)


def apply_claimed_step_update(
    db: HubDatabase,
    agent_body: AgentDefinitionBody,
    *,
    session_id: str,
    initial_variables: dict[str, Any] | None = None,
) -> None:
    """Atomically advance a persisted instance after a successful auto-claim."""
    snapshot = _require_step_workflow(agent_body)
    current_step, variables = initial_step_state_for_spawn(
        snapshot,
        agent_name=agent_body.name,
        task_owned_by_child=True,
        initial_variables=initial_variables,
    )
    manager = AgentStepInstanceManager(db)
    instance = manager.get_for_session(session_id)
    if instance is None:
        raise RuntimeError(f"missing step instance for claimed spawn session {session_id}")
    instance.current_step = current_step
    instance.variables = variables
    manager.save(instance)

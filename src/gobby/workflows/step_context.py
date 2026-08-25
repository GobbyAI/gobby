"""Helpers for inspecting active step-workflow state."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.step_instances import AgentStepInstance, AgentStepInstanceManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StepWorkflowContext:
    """Current step details for a session-owned step workflow."""

    workflow_name: str
    current_step: str
    description: str | None
    status_message: str | None
    exit_condition: str | None
    agent_name: str | None = None
    allowed_tools: list[str] | Literal["all"] = "all"
    is_entry_step: bool = False


@dataclass(frozen=True)
class IncompleteStepWorkflow:
    """An active step workflow that has not reached its exit condition."""

    workflow_name: str
    current_step: str
    exit_condition: str | None
    eval_error: Exception | None = None
    agent_name: str | None = None


def get_active_step_workflow_context(
    db: HubDatabase,
    session_id: str | None,
) -> StepWorkflowContext | None:
    """Return the first active step-workflow context using synchronous DB reads."""
    if not session_id:
        return None
    return _get_active_step_workflow_context(db, session_id)


def _get_active_step_workflow_context(
    db: HubDatabase,
    session_id: str,
) -> StepWorkflowContext | None:
    """Read step workflow state, ignoring malformed workflow definitions."""

    for instance in _iter_active_step_instances(db, session_id):
        if not instance.current_step:
            continue
        step = instance.snapshot.get_step(instance.current_step)
        if step is None:
            continue

        return StepWorkflowContext(
            workflow_name=instance.agent_name,
            current_step=instance.current_step,
            description=step.description,
            status_message=step.status_message,
            exit_condition=instance.snapshot.exit_condition,
            agent_name=instance.agent_name,
            allowed_tools=step.allowed_tools,
            is_entry_step=bool(
                instance.snapshot.steps and instance.current_step == instance.snapshot.steps[0].name
            ),
        )

    return None


def _iter_active_step_instances(
    db: HubDatabase,
    session_id: str,
) -> Iterator[AgentStepInstance]:
    instance = AgentStepInstanceManager(db).get_for_session(session_id)
    if instance is None or not instance.enabled:
        return
    yield instance


def first_incomplete_step_workflow(
    db: HubDatabase,
    session_id: str,
) -> IncompleteStepWorkflow | None:
    """Return the first active step workflow whose exit condition is not satisfied.

    ``None`` means no active step workflow is holding the session open — either
    every active instance reached its exit condition or the session owns none.
    Callers that need to distinguish those cases pair this with
    :func:`get_active_step_workflow_context`.
    """
    from gobby.workflows.safe_evaluator import SafeExpressionEvaluator, build_condition_helpers
    from gobby.workflows.state_manager import SessionVariableManager

    session_variables = SessionVariableManager(db).get_variables(session_id)

    for instance in _iter_active_step_instances(db, session_id):
        variables = {**session_variables, **instance.variables}
        if variables.get("step_workflow_complete") is True:
            continue
        if not instance.snapshot.steps:
            continue

        if not instance.snapshot.exit_condition:
            return IncompleteStepWorkflow(
                workflow_name=instance.agent_name,
                current_step=instance.current_step or "",
                exit_condition=instance.snapshot.exit_condition,
                agent_name=instance.agent_name,
            )

        ctx = {
            "current_step": instance.current_step,
            "vars": variables,
            "variables": variables,
        }
        try:
            exit_met = SafeExpressionEvaluator(
                context=ctx,
                allowed_funcs=build_condition_helpers(context=ctx),
            ).evaluate(instance.snapshot.exit_condition)
        except Exception as exc:
            return IncompleteStepWorkflow(
                workflow_name=instance.agent_name,
                current_step=instance.current_step or "",
                exit_condition=instance.snapshot.exit_condition,
                eval_error=exc,
                agent_name=instance.agent_name,
            )

        if not exit_met:
            return IncompleteStepWorkflow(
                workflow_name=instance.agent_name,
                current_step=instance.current_step or "",
                exit_condition=instance.snapshot.exit_condition,
                agent_name=instance.agent_name,
            )

    return None


def has_active_step_workflow(db: HubDatabase, session_id: str | None) -> bool:
    """Return whether the session has an active step workflow via synchronous DB reads."""
    return get_active_step_workflow_context(db, session_id) is not None

"""Keep seat task bindings aligned with live claims."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.tasks.state_semantics import is_task_claimed_by_session
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.step_instances import (
    AgentStepInstance,
    AgentStepInstanceManager,
    StaleStepInstanceWriteError,
)
from gobby.workflows.task_claim_state import active_task_id_for_edit, resolve_target_task_id

BOUND_TASK_VARIABLE = "assigned_task_id"


def _binding_step_index(snapshot: AgentStepWorkflowBody) -> int | None:
    """Index of the step whose claim_task handler binds the task."""
    for index, step in enumerate(snapshot.steps):
        for handler in step.on_mcp_success:
            if (
                handler.get("server") == "gobby-tasks"
                and handler.get("tool") == "claim_task"
                and handler.get("variable") == BOUND_TASK_VARIABLE
            ):
                return index
    return None


def _read_task(db: HubDatabase, task_id: str) -> Task | None:
    try:
        return LocalTaskManager(db).get_task(task_id)
    except (TaskNotFoundError, ValueError):
        return None


def pending_claim_handlers(instance: AgentStepInstance) -> list[dict[str, Any]] | None:
    """Bind an early successful claim without skipping the steps before claim."""
    workflow = instance.workflow
    binding = _binding_step_index(workflow)
    names = [step.name for step in workflow.steps]
    if binding is None or instance.current_step not in names:
        return None
    if names.index(instance.current_step) >= binding:
        return None
    return workflow.steps[binding].on_mcp_success


def release_lost_binding(
    db: HubDatabase,
    instance: AgentStepInstance,
    session_variables: dict[str, Any],
    *,
    reset_on_claim: bool = False,
    authoritative: bool = False,
) -> AgentStepInstance:
    """Rewind a seat when its claim is lost or another owned claim becomes active.

    An unchanged held binding costs no task query. A different active selection
    must still be authoritatively owned before it can replace a parked binding.
    A selected successful claim completion resets even an unchanged binding. An authoritative
    check (BEFORE_TOOL) reads the task instead of trusting the projected claim map, which
    another actor's close or release leaves stale until the next reconciliation.
    """
    snapshot = instance.workflow
    binding = _binding_step_index(snapshot)
    names = [step.name for step in snapshot.steps]
    if binding is None or instance.current_step not in names:
        return instance
    if names.index(instance.current_step) <= binding:
        return instance
    bound = {**session_variables, **instance.variables}.get(BOUND_TASK_VARIABLE)
    if not bound:
        return instance
    active = active_task_id_for_edit(session_variables)
    bound_id = resolve_target_task_id(session_variables, bound) or str(bound)
    selected = _read_task(db, active) if active is not None and active != bound_id else None
    if selected is None or not is_task_claimed_by_session(selected, instance.session_id):
        claimed = None if authoritative else session_variables.get("claimed_tasks")
        if (
            not reset_on_claim
            and isinstance(claimed, dict)
            and (bound in claimed or bound in claimed.values())
        ):
            return instance
        task = _read_task(db, str(bound))
        if reset_on_claim and (
            active != bound_id
            or task is None
            or not is_task_claimed_by_session(task, instance.session_id)
        ):
            return instance
        if (
            not reset_on_claim
            and task is not None
            and is_task_claimed_by_session(task, instance.session_id)
        ):
            return instance

    rewound = instance.model_copy(deep=True)
    defaults = copy.deepcopy(snapshot.variables)
    rewound.variables.update(defaults)
    rewound.current_step = names[binding]
    rewound.step_action_count = 0
    rewound.step_entered_at = datetime.now(UTC)
    try:
        AgentStepInstanceManager(db).save(rewound, if_match=(instance.id, instance.updated_at))
    except StaleStepInstanceWriteError:
        return instance
    return rewound


def reconcile_active_binding(db: HubDatabase, session_id: str, variables: dict[str, Any]) -> None:
    """Rewind the session's seat before its BEFORE_TOOL allow-list is judged."""
    instance = AgentStepInstanceManager(db).get_active_for_session(session_id)
    if instance is not None and instance.enabled and instance.current_step:
        release_lost_binding(db, instance, variables, authoritative=True)

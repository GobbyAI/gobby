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
) -> AgentStepInstance:
    """Rewind a seat when its claim is lost or another owned claim becomes active.

    An unchanged held binding costs no task query. A different active selection
    must still be authoritatively owned before it can replace a parked binding.
    A selected successful claim completion resets even an unchanged binding.
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
        claimed = session_variables.get("claimed_tasks")
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


def recover_rejected_submission(db: HubDatabase, instance: AgentStepInstance) -> AgentStepInstance:
    """Recover a developer whose current candidate was rejected without a message read.

    Review records are authoritative even when a hold delayed the reviewer or its
    notification. Require evidence from this submission; a previous round must not
    reopen a freshly resubmitted candidate.
    """
    if (
        instance.agent_name != "developer"
        or instance.current_step != "submit"
        or instance.workflow.get_step("implement") is None
        or instance.step_entered_at is None
    ):
        return instance
    task_id = instance.variables.get(BOUND_TASK_VARIABLE)
    task = _read_task(db, task_id) if isinstance(task_id, str) else None
    if (
        task is None
        or not is_task_claimed_by_session(task, instance.session_id)
        or not task.commits
    ):
        return instance

    from gobby.agents.team_lifecycle import seat_team
    from gobby.agents.team_review import seat_team_held
    from gobby.tasks.step_records import current_task_steps, list_task_step_records

    steps = current_task_steps(list_task_step_records(db, task.id), task.review_epoch)
    review, implementation = steps.get("review"), steps.get("implementation")
    if (
        review is None
        or review.status != "rejected"
        or review.updated_at < instance.step_entered_at
        or implementation is None
        or implementation.status != "approved"
        or implementation.session_id != instance.session_id
        or not review.commit_sha
        or implementation.commit_sha != review.commit_sha
        or not review.commit_sha.startswith(task.commits[-1])
    ):
        return instance
    binding = seat_team(db, instance.session_id)
    if binding is not None and seat_team_held(db, binding):
        return instance

    recovered = instance.model_copy(deep=True)
    recovered.variables.update(bounced=True, implementation_complete=False)
    recovered.current_step = "implement"
    recovered.step_action_count = 0
    recovered.step_entered_at = datetime.now(UTC)
    try:
        AgentStepInstanceManager(db).save(recovered, if_match=(instance.id, instance.updated_at))
    except StaleStepInstanceWriteError:
        return instance
    return recovered

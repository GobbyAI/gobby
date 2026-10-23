"""Keep seat task bindings aligned with live claims and landing receipts."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.tasks.close_receipts import LANDING, list_close_receipts
from gobby.tasks.state_semantics import is_task_claimed_by_session
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.reserved_variables import DEVELOPER_HANDOFF_TASKS_VARIABLE
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import (
    AgentStepInstance,
    AgentStepInstanceManager,
    StaleStepInstanceWriteError,
)
from gobby.workflows.task_claim_state import active_task_id_for_edit, resolve_target_task_id

BOUND_TASK_VARIABLE = "assigned_task_id"
_CANDIDATE_LATCH_VARIABLES = (
    "candidate_sent",
    "implementation_complete",
    BOUND_TASK_VARIABLE,
    "assigned_task_ref",
)


def _unbounced_submitted_candidate(instance: AgentStepInstance) -> bool:
    """A developer's sent candidate that no reviewer has bounced."""
    return (
        instance.agent_name == "developer"
        and instance.current_step == "submit"
        and bool(instance.variables.get("candidate_sent"))
        and not instance.variables.get("bounced")
    )


def release_completed_review(db: HubDatabase, instance: AgentStepInstance) -> AgentStepInstance:
    """Recover an interrupted verdict only from this reviewer's exact landing."""
    if instance.agent_name != "code-reviewer" or instance.current_step != "verdict":
        return instance
    state = instance.variables
    task_id, sha = state.get("candidate_task"), state.get("candidate_sha")
    if (
        not state.get("candidate_received")
        or not isinstance(task_id, str)
        or not isinstance(sha, str)
        or instance.workflow.get_step("await") is None
    ):
        return instance
    if not any(
        receipt.kind == LANDING
        and receipt.commit_sha == sha
        and receipt.author_session_id == instance.session_id
        for receipt in list_close_receipts(db, task_id)
    ):
        return instance

    recovered = instance.model_copy(deep=True)
    defaults = {
        key: copy.deepcopy(instance.workflow.variables.get(key))
        for key in (
            "candidate_received",
            "verdict_ready",
            "candidate_task",
            "candidate_sha",
            "candidate_owner",
            "candidate_received_at",
        )
    }
    recovered.variables.update(defaults)
    recovered.current_step = "await"
    recovered.step_action_count = 0
    recovered.step_entered_at = datetime.now(UTC)
    try:
        AgentStepInstanceManager(db).save(recovered, if_match=(instance.id, instance.updated_at))
    except StaleStepInstanceWriteError:
        return instance
    SessionVariableManager(db).merge_variables(instance.session_id, defaults)
    return recovered


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


def release_lost_binding(
    db: HubDatabase,
    instance: AgentStepInstance,
    session_variables: dict[str, Any],
) -> AgentStepInstance:
    """Rewind a seat when its claim is lost or another owned claim becomes active.

    An unchanged held binding costs no task query. A different active selection
    must still be authoritatively owned before it can replace a parked binding.
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
        if isinstance(claimed, dict) and (bound in claimed or bound in claimed.values()):
            return instance
        task = _read_task(db, str(bound))
        if task is not None and is_task_claimed_by_session(task, instance.session_id):
            return instance

    rewound = instance.model_copy(deep=True)
    defaults = copy.deepcopy(snapshot.variables)
    # Handoffs outlive any one binding; only observing their close retires them.
    defaults.pop(DEVELOPER_HANDOFF_TASKS_VARIABLE, None)
    if _unbounced_submitted_candidate(instance):
        # A release is not a bounce: a same-task re-claim resumes the candidate at submit.
        for name in _CANDIDATE_LATCH_VARIABLES:
            defaults[name] = instance.variables.get(name, defaults.get(name))
    rewound.variables.update(defaults)
    rewound.current_step = names[binding]
    rewound.step_action_count = 0
    rewound.step_entered_at = datetime.now(UTC)
    try:
        AgentStepInstanceManager(db).save(rewound, if_match=(instance.id, instance.updated_at))
    except StaleStepInstanceWriteError:
        return instance
    return rewound

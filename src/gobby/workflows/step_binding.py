"""Keep a step workflow's task binding in line with the live task claim."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.tasks.state_semantics import is_task_claimed_by_session
from gobby.workflows.agent_models import AgentStepWorkflowBody
from gobby.workflows.reserved_variables import DEVELOPER_HANDOFF_TASKS_VARIABLE
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
    rewound.variables.update(defaults)
    rewound.current_step = names[binding]
    rewound.step_action_count = 0
    rewound.step_entered_at = datetime.now(UTC)
    try:
        AgentStepInstanceManager(db).save(rewound, if_match=(instance.id, instance.updated_at))
    except StaleStepInstanceWriteError:
        return instance
    return rewound

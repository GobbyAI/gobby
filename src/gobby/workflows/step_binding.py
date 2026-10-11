"""Keep a step workflow's task binding in line with the live task claim."""

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


def bound_task_commits(db: HubDatabase, task_id: str) -> list[str]:
    """Commits already linked to the bound task."""
    task = _read_task(db, task_id)
    return list(task.commits or []) if task is not None else []


def release_lost_binding(
    db: HubDatabase,
    instance: AgentStepInstance,
    session_variables: dict[str, Any],
) -> AgentStepInstance:
    """Return a seat past its binding step to that step once its task claim is gone.

    The session's ``claimed_tasks`` projection gates the task read, so a binding
    the session still holds costs no query.
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
    claimed = session_variables.get("claimed_tasks")
    if isinstance(claimed, dict) and (bound in claimed or bound in claimed.values()):
        return instance
    task = _read_task(db, str(bound))
    if task is not None and is_task_claimed_by_session(task, instance.session_id):
        return instance

    rewound = instance.model_copy(deep=True)
    rewound.variables.update(copy.deepcopy(snapshot.variables))
    rewound.current_step = names[binding]
    rewound.step_action_count = 0
    rewound.step_entered_at = datetime.now(UTC)
    try:
        AgentStepInstanceManager(db).save(rewound, if_match=(instance.id, instance.updated_at))
    except StaleStepInstanceWriteError:
        return instance
    return rewound

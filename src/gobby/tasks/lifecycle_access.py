"""Reference resolution for authorized post-implementation task operations."""

from __future__ import annotations

from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.state_manager import SessionVariableManager


class CrossProjectTaskForbidden(ValueError):
    """The authenticated caller has no authority over a foreign task."""

    def __init__(self) -> None:
        # Preserve the not-found contract for refs outside the caller authority.
        super().__init__("task_not_found")


def is_operator_coordinator(tasks: LocalTaskManager, caller: Session) -> bool:
    """Recognize an enabled operator role on a locally owned root session."""
    if caller.parent_session_id is not None or caller.machine_id != require_machine_id():
        return False
    role = SessionVariableManager(tasks.db).get_variable_subset(caller.id, ("_agent_type",))
    agent_type = role.get("_agent_type")
    if not isinstance(agent_type, str) or agent_type not in {"assistant", "orchestrator"}:
        return False
    definition = AgentDefinitionManager(tasks.db).get_by_name(agent_type, caller.project_id)
    return definition is not None and definition.enabled


def resolve_lifecycle_task(
    tasks: LocalTaskManager,
    ref: str,
    *,
    project_id: str,
    caller_session_id: str | None,
    sessions: SessionManager | None,
) -> Task:
    """Resolve local refs normally, and foreign refs only for local coordinators.

    This does not authorize claiming or bypass landing/receipt policy. Foreign
    access requires a descendant owning the task or an enabled operator role on
    a root session. Identity and lineage come exclusively from stored records.
    """
    target_project = project_id
    local_ref = ref
    if "#" in ref and not ref.startswith("#"):
        project_ref, number = ref.rsplit("#", 1)
        project = LocalProjectManager(tasks.db).resolve_ref(project_ref)
        if project is None or not number.isdigit():
            raise TaskNotFoundError(f"Task {ref} not found")
        target_project = project.id
        local_ref = f"#{number}"
    if (
        local_ref.startswith("#")
        or local_ref.isdigit()
        or all(part.isdigit() for part in local_ref.split("."))
    ):
        task_id = tasks.resolve_task_reference(local_ref, target_project)
    else:
        task_id = local_ref
    task = tasks.get_task(task_id)
    if task.project_id == project_id:
        return task
    if sessions is None or not caller_session_id:
        raise CrossProjectTaskForbidden()
    caller = sessions.get(caller_session_id)
    machine_id = require_machine_id()
    if caller is None or caller.project_id != project_id or caller.machine_id != machine_id:
        raise CrossProjectTaskForbidden()
    if (
        tasks.db.fetchone(
            "SELECT 1 FROM sessions WHERE project_id = %s AND machine_id = %s LIMIT 1",
            (task.project_id, machine_id),
        )
        is None
    ):
        raise CrossProjectTaskForbidden()
    owners = (
        task.closed_in_session_id,
        task.claimed_by_session_id,
        task.created_in_session_id,
        task.delegated_by_session_id,
    )
    for owner_id in owners:
        if owner_id is None:
            continue
        owner = sessions.get(owner_id)
        if (
            owner is not None
            and owner.machine_id == machine_id
            and sessions.is_ancestor(caller.id, owner.id)
        ):
            return task
    if is_operator_coordinator(tasks, caller):
        return task
    raise CrossProjectTaskForbidden()

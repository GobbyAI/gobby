"""Reference resolution for authorized post-implementation task operations."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.agent_models import AgentTaskScope
from gobby.workflows.agent_resolver import session_agent_definition


class CrossProjectTaskForbidden(ValueError):
    """The authenticated caller has no authority over a foreign task."""

    def __init__(self) -> None:
        # Preserve the not-found contract for refs outside the caller authority.
        super().__init__("task_not_found")


def session_grants(db: HubDatabase, session_id: str, step: str) -> bool:
    """Whether the enabled definition a session runs as grants ``step`` records."""
    session = SessionManager(db).get(session_id)
    resolved = session_agent_definition(db, session) if session is not None else None
    return resolved is not None and step in resolved[0].task_step_writes


def task_scope_refusal(task: Task, session_id: str, scope: AgentTaskScope) -> str | None:
    """Explain which declared task constraint excludes the operation, if any."""
    if scope.task_types is not None and task.task_type not in scope.task_types:
        return f"The definition's task scope excludes type {task.task_type}."
    if scope.categories is not None and task.category not in scope.categories:
        return f"The definition's task scope excludes category {task.category}."
    if scope.own_claim_only and task.claimed_by_session_id != session_id:
        return "The definition's task scope requires the caller's own claim."
    return None


def is_operator_coordinator(
    tasks: LocalTaskManager, caller: Session, *, capability: str = "coordinate_tasks"
) -> bool:
    """Check a definition's coordinator permission on a locally owned root session."""
    # Clear successors have parents for lineage; run/depth identify spawned seats.
    if caller.agent_run_id or caller.agent_depth or caller.machine_id != require_machine_id():
        return False
    definition = session_agent_definition(tasks.db, caller)
    return definition is not None and capability in definition[0].capabilities


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
    access requires a descendant owning the task or the coordinate_tasks capability
    on a root session. Identity and lineage come exclusively from stored records.
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

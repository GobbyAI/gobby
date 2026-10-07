"""Admission of commit paths against live task ownership."""

from gobby.storage.tasks import LocalTaskManager, Task
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import assert_task_edit_paths_available


def assert_task_commit_paths_available(
    task_manager: LocalTaskManager, task: Task, paths: set[str], checkout_root: str
) -> None:
    """Refuse a candidate that contains a different active claim's live paths."""
    if not paths:
        return
    canonical = task_manager.get_task(task.id)
    owner = canonical.claimed_by_session_id
    if owner is None:
        return
    variables = SessionVariableManager(task_manager.db).get_variables(owner)
    try:
        assert_task_edit_paths_available(variables, task.id, sorted(paths), checkout_root)
    except ValueError as exc:
        raise ValueError(f"Cannot link or close this commit; split changes by task. {exc}") from exc

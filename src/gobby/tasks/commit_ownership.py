"""Admission of commit paths against live task ownership."""

from pathlib import Path

from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.daemon_git import GitOk, daemon_git
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


async def assert_auto_link_commit_available(
    task_manager: LocalTaskManager, task: Task, commit_sha: str, cwd: str | Path | None
) -> None:
    """Check each automatically discovered commit before it enters task evidence."""
    canonical = task_manager.get_task(task.id)
    if not isinstance(canonical.claimed_by_session_id, str):
        return
    checkout_root = Path(cwd).resolve() if cwd is not None else Path.cwd()
    result = await daemon_git.run(
        ["show", "--first-parent", "--format=", "--name-only", "-z", commit_sha],
        cwd=checkout_root,
        timeout=30,
    )
    if not isinstance(result, GitOk):
        raise ValueError("Cannot prove automatically discovered commit path ownership.")
    paths = {path for path in result.stdout.split("\0") if path}
    assert_task_commit_paths_available(task_manager, canonical, paths, str(checkout_root))

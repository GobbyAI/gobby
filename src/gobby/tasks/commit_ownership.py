"""Admission of commit paths against live task ownership."""

from pathlib import Path

from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.daemon_git import GitOk, daemon_git
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.task_claim_state import assert_task_edit_paths_available


def assert_task_commit_paths_available(
    task_manager: LocalTaskManager,
    task: Task,
    paths: set[str],
    checkout_root: str,
    *,
    commit_path_times: dict[str, float] | None = None,
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
        assert_task_edit_paths_available(
            variables, task.id, sorted(paths), checkout_root, commit_path_times=commit_path_times
        )
    except ValueError as exc:
        raise ValueError(f"Cannot link or close this commit; split changes by task. {exc}") from exc


async def assert_task_commit_paths_available_async(
    task_manager: LocalTaskManager,
    task: Task,
    paths: set[str],
    checkout_root: str,
    commit_shas: list[str],
) -> None:
    """A competing edit that began after a path's commit cannot contaminate it."""
    try:
        assert_task_commit_paths_available(task_manager, task, paths, checkout_root)
        return
    except ValueError as exc:
        conflict = str(exc)
    times: dict[str, float] = {}
    for sha in commit_shas:
        result = await daemon_git.run(
            ["show", "--first-parent", "--no-renames", "--format=%ct", "--name-only", "-z", sha],
            cwd=checkout_root,
            timeout=30,
        )
        if not isinstance(result, GitOk):
            raise ValueError(f"{conflict} Cannot prove overlapping paths' commit chronology.")
        fields = result.stdout.split("\0")
        try:
            stamp = float(fields[0].strip())
        except ValueError as exc:
            raise ValueError(
                f"{conflict} Cannot prove overlapping paths' commit chronology."
            ) from exc
        for field in fields[1:]:
            path = field.removeprefix("\n")
            if path in paths:
                times[path] = max(times.get(path, stamp), stamp)
    assert_task_commit_paths_available(
        task_manager, task, paths, checkout_root, commit_path_times=times
    )


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
    await assert_task_commit_paths_available_async(
        task_manager, canonical, paths, str(checkout_root), [commit_sha]
    )

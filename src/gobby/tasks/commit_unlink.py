"""Audited unlink of a commit from a task.

A commit on the integration branch is delivered work and stays linked. Every other
unlink writes a task lifecycle row that names the actor and the reason, so a no-work
close never rests on a removal nobody recorded.
"""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.tasks._lifecycle_events import TaskLifecycleEventManager
from gobby.utils.daemon_git import GitFailed, GitOk, daemon_git


class CommitUnlinkRefused(ValueError):
    """The commit cannot leave the task: it is landed, or its landing status is unproven."""


async def integration_refusal(db: HubDatabase, task: Task, sha: str) -> str | None:
    """Return why unlinking ``sha`` is refused, or None when ``sha`` is not landed.

    With no registered main checkout or no integration branch, nothing can be landed, so the
    unlink proceeds. A Git failure or an unanswered ancestry check refuses, because the
    commit's status is then unproven.
    """
    from gobby.tasks.land_commit import LandingGitError, main_checkout_target

    try:
        target = await main_checkout_target(db, task.project_id)
    except LandingGitError as exc:
        return f"Cannot determine the integration branch: {exc}"
    except ValueError:
        return None
    if target is None:
        return None
    repo, branch = target
    result = await daemon_git.run(
        ("merge-base", "--is-ancestor", sha, f"refs/heads/{branch}"), cwd=repo
    )
    if isinstance(result, GitOk):
        return f"{sha} is on the integration branch; landed commits cannot be unlinked."
    if isinstance(result, GitFailed) and result.returncode == 1:
        return None
    return f"Cannot determine whether {sha} is on the integration branch."


async def unlink_commit_audited(
    task_manager: LocalTaskManager,
    task: Task,
    commit_sha: str,
    *,
    reason: str,
    actor: str,
) -> Task:
    """Unlink ``commit_sha`` from ``task`` and record who removed it and why.

    Raises CommitUnlinkRefused for a landed or unproven commit and ValueError when the
    commit is not linked. Blank reason or actor is refused before any mutation. The
    removal and its audit row share one transaction, so a failed audit write keeps every
    link and records no event.
    """
    if not reason.strip():
        raise ValueError("A reason is required to unlink a commit.")
    if not actor.strip():
        raise ValueError("An actor is required to unlink a commit.")
    refusal = await integration_refusal(task_manager.db, task, commit_sha)
    if refusal is not None:
        raise CommitUnlinkRefused(refusal)
    with task_manager.db.transaction():
        updated = task_manager.unlink_commit(task.id, commit_sha)
        TaskLifecycleEventManager(task_manager.db).record_lifecycle_event(
            task.id,
            "commit:linked",
            "commit:unlinked",
            f"Unlinked {commit_sha}: {reason.strip()}",
            by_actor=actor.strip(),
        )
    return updated

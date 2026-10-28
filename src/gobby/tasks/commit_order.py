"""Keep a task's linked commits ordered so the last entry is the tip the review gate reads."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from gobby.storage.tasks import LocalTaskManager


class HistoryCommit(Protocol):
    @property
    def sha(self) -> str: ...


def same_commit(left: str, right: str) -> bool:
    """Match two SHAs as the storage link queries do: equal, or a prefix of at least seven."""
    left, right = left.lower(), right.lower()
    if left == right:
        return True
    return min(len(left), len(right)) >= 7 and (left.startswith(right) or right.startswith(left))


def link_by_history(
    task_manager: LocalTaskManager,
    task_id: str,
    commit_sha: str,
    linked: Sequence[str],
    history: Sequence[HistoryCommit],
) -> list[str]:
    """Link ``commit_sha``, a commit from ``history``, keeping the tip the newest linked commit.

    ``linked`` is the task's commits before this link. Appending an older commit after a
    later tip would demote that tip, so when history places the tip after ``commit_sha``,
    the tip moves back to the end. History is the chronological order ``git log --reverse``
    reports, so this relies on that order matching ancestry for the task's commits.

    Returns the commits as the manager stored them after the link, so the caller's list
    tracks the database order rather than its own appends.
    """
    shas = [entry.sha for entry in history]
    position = shas.index(commit_sha)
    tip = linked[-1] if linked else None
    task = task_manager.link_commit(task_id, commit_sha)
    if tip is not None:
        tip_position = next(
            (index for index, sha in enumerate(shas) if same_commit(sha, tip)), None
        )
        if tip_position is not None and tip_position > position:
            task = task_manager.move_commit_to_tip(task_id, tip)
    return list(task.commits or [])

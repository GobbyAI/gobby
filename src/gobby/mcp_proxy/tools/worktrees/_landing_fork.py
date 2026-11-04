"""The fork a landed branch last took from its target, read from the landing merge."""

from __future__ import annotations

import re

from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.close_receipts import list_close_receipts
from gobby.utils.git import run_to_completion
from gobby.worktrees.git.manager import WorktreeGitManager

_FULL_SHA = re.compile(r"[0-9a-f]{40}")


async def landed_source_fork(
    git: WorktreeGitManager, cwd: str, source_ref: str, target_ref: str
) -> str | None:
    """Return merge-base(source, target just before the landing merge), or None.

    A landing merge keeps the pre-landing target as its first parent, so this
    fork reflects every base merge or rebase the branch took before landing. A
    fast-forward landing leaves no landing merge and returns None.
    """
    landings = await run_to_completion(
        git.run_git_command(
            [
                "rev-list",
                "--first-parent",
                "--ancestry-path",
                "--reverse",
                f"{source_ref}..{target_ref}",
            ],
            cwd=cwd,
            timeout=10,
        )
    )
    oldest = landings.stdout.split()[:1] if landings.returncode == 0 else []
    if not oldest:
        return None
    before_landing = f"{oldest[0]}^1"
    contained = await run_to_completion(
        git.run_git_command(
            ["merge-base", "--is-ancestor", source_ref, before_landing], cwd=cwd, timeout=10
        )
    )
    if contained.returncode != 1:
        # 0: the target already held the source, so the landing was a fast-forward.
        return None
    fork = await run_to_completion(
        git.run_git_command(["merge-base", source_ref, before_landing], cwd=cwd, timeout=10)
    )
    sha = fork.stdout.strip() if fork.returncode == 0 else ""
    return sha or None


def receipt_integration_base(db: HubDatabase, task_id: str, landed_sha: str) -> str | None:
    """Return the target tip a landing receipt recorded before landing ``landed_sha``.

    A fast-forward landing leaves no merge to read the pre-landing target from;
    the task's landing receipt keeps it as ``integration_base``.
    """
    for receipt in reversed(list_close_receipts(db, task_id)):
        if landed_sha not in (receipt.commit_sha, receipt.facts.get("landed_tip")):
            continue
        base = receipt.facts.get("integration_base")
        if isinstance(base, str) and _FULL_SHA.fullmatch(base):
            return base
    return None

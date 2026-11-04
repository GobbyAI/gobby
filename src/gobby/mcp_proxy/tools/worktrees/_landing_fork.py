"""The fork a landed branch last took from its target, read from the landing merge."""

from __future__ import annotations

from gobby.utils.git import run_to_completion
from gobby.worktrees.git.manager import WorktreeGitManager


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

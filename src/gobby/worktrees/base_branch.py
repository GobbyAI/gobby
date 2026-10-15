"""Branch-name rules for worktrees.base_branch."""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import Any

from gobby.storage.worktrees import Worktree

_SHA_BASE = re.compile(r"^[0-9a-fA-F]{10,40}$")


class WorktreeBaseError(RuntimeError):
    """A requested fork cannot provide a branch and an exact commit."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


async def select_worktree_base(git_manager: Any, requested_base: str | None) -> tuple[str, str]:
    """Capture one fork commit; only explicitly remote refs fetch."""
    if requested_base is None:
        branch = await git_manager.get_current_branch()
        if not branch:
            raise WorktreeBaseError(
                "detached_head_requires_base",
                "Detached HEAD requires an explicit base branch; retry with base_branch.",
            )
        ref = "HEAD"
    else:
        if await rejects_unreferenced_sha_base(git_manager, requested_base):
            raise WorktreeBaseError(
                "base_branch_is_commit_sha", "base_branch must name a branch, not a commit sha"
            )
        remote = requested_base.startswith(("origin/", "refs/remotes/origin/"))
        branch = requested_base.removeprefix("refs/remotes/origin/").removeprefix("origin/")
        branch = branch.removeprefix("refs/heads/")
        if branch.startswith("refs/"):
            raise WorktreeBaseError("invalid_base_ref", f"Unsupported base ref: {requested_base}")
        valid = await git_manager.run_git_command(
            ["check-ref-format", "--branch", branch], timeout=5
        )
        if valid.returncode != 0:
            raise WorktreeBaseError("invalid_base_ref", f"Invalid base branch: {requested_base}")
        ref = f"refs/heads/{branch}"
        if remote:
            ref = f"refs/remotes/origin/{branch}"
            fetched = await git_manager.run_git_command(
                ["fetch", "origin", f"+refs/heads/{branch}:{ref}"], timeout=60
            )
            if fetched.returncode != 0:
                raise WorktreeBaseError(
                    "base_ref_unavailable",
                    f"Failed to fetch {requested_base}: {fetched.stderr.strip()}",
                )
    commit = await git_manager.get_local_commit(f"{ref}^{{commit}}")
    if not commit:
        raise WorktreeBaseError("base_ref_unavailable", f"Cannot resolve selected base {ref}")
    return branch, commit


def is_sha_shaped(base_branch: str) -> bool:
    """Return whether base_branch has the 10-to-40 character hex shape."""
    return _SHA_BASE.fullmatch(base_branch) is not None


def is_unreferenced_sha_base(base_branch: str, ref_names: Collection[str]) -> bool:
    """Return whether base_branch is a hex sha with no matching ref name."""
    return is_sha_shaped(base_branch) and base_branch not in ref_names


def name_unreferenced_sha_base_rows(
    rows: Sequence[Worktree],
    ref_names: Collection[str],
) -> list[Worktree]:
    """Name rows whose base_branch is a commit sha with no matching ref."""
    return [row for row in rows if is_unreferenced_sha_base(row.base_branch, ref_names)]


async def rejects_unreferenced_sha_base(git_manager: Any, base_branch: str) -> bool:
    """Return whether creation must refuse to store this base_branch."""
    if not is_sha_shaped(base_branch):
        return False
    result = await git_manager.run_git_command(
        ["for-each-ref", "--format=%(refname:short)"],
        timeout=10,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "git for-each-ref failed"
        raise RuntimeError(detail)
    ref_names = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return base_branch not in ref_names

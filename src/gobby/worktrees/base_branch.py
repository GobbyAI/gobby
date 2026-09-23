"""Branch-name rules for worktrees.base_branch."""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import Any

from gobby.storage.worktrees import Worktree

_SHA_BASE = re.compile(r"^[0-9a-fA-F]{10,40}$")


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

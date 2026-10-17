"""Prove that a differing validation tree is already part of shipped integration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from gobby.utils.git import run_git_command


def _git(root: str | Path, *args: str) -> str | None:
    return run_git_command(["git", *args], cwd=root)


@dataclass
class LandedCandidateCredit:
    """Bound mutable Git observations to a single close evaluation, never a global cache."""

    root: str
    candidate: str
    _results: dict[str, str | None] = field(default_factory=dict)

    def credits(self, tree: str) -> bool:
        if tree not in self._results:
            self._results[tree] = self._refusal(tree)
        return self._results[tree] is None

    @property
    def rejections(self) -> tuple[str, ...]:
        return tuple(f"{tree}: {reason}" for tree, reason in self._results.items() if reason)

    def _refusal(self, tree: str) -> str | None:
        # Landing protects the branch checked out in the common repository's main
        # checkout (land_commit.main_checkout_target), not a branch selected by a run.
        common = _git(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if not common:
            return "integration checkout could not be resolved"
        main = Path(common).parent
        branch = _git(main, "symbolic-ref", "--quiet", "HEAD")
        tip = _git(main, "rev-parse", "--verify", branch) if branch else None
        if not tip:
            return "integration branch could not be resolved"
        if _git(main, "merge-base", "--is-ancestor", self.candidate, tip) is None:
            return "candidate is not landed on the integration branch"
        head = _git(tree, "rev-parse", "--verify", "HEAD")
        top = _git(tree, "rev-parse", "--show-toplevel")
        if not head or not top or os.path.realpath(top) != os.path.realpath(tree):
            return "run checkout HEAD could not be resolved"
        if (
            _git(main, "merge-base", "--is-ancestor", self.candidate, head) is None
            or _git(main, "merge-base", "--is-ancestor", head, tip) is None
        ):
            return "run HEAD is not on the integration branch after the candidate"
        status = _git(tree, "status", "--porcelain=v1", "--untracked-files=all")
        if status is None:
            return "run checkout cleanliness could not be verified"
        if status:
            return "run checkout is dirty"
        # The clean worktree is exactly HEAD; candidate <= HEAD <= integration tip
        # proves every differing path comes from later landed commits, including
        # deletions and merge resolutions. Unlanded descendants cannot pass this.
        return None

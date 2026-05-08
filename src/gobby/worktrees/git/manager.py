"""Public `WorktreeGitManager` facade."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from gobby.worktrees.git import _branch, _lifecycle, _locking, _merge, _status
from gobby.worktrees.git._models import GitOperationResult, WorktreeInfo, WorktreeStatus
from gobby.worktrees.git._runner import GitRunner


class WorktreeGitManager(GitRunner):
    """
    Manager for git worktree operations.

    Provides methods to create, delete, and manage git worktrees.
    All operations are performed relative to a base repository path.
    """

    def create_worktree(
        self,
        worktree_path: str | Path,
        branch_name: str,
        base_branch: str = "main",
        create_branch: bool = True,
        use_local: bool = False,
    ) -> GitOperationResult:
        return _lifecycle.create_worktree(
            self,
            worktree_path,
            branch_name,
            base_branch=base_branch,
            create_branch=create_branch,
            use_local=use_local,
        )

    def delete_worktree(
        self,
        worktree_path: str | Path,
        force: bool = False,
        delete_branch: bool = False,
        branch_name: str | None = None,
    ) -> GitOperationResult:
        return _lifecycle.delete_worktree(
            self,
            worktree_path,
            force=force,
            delete_branch=delete_branch,
            branch_name=branch_name,
        )

    def sync_from_main(
        self,
        worktree_path: str | Path,
        base_branch: str = "main",
        strategy: Literal["rebase", "merge"] = "rebase",
    ) -> GitOperationResult:
        return _lifecycle.sync_from_main(
            self, worktree_path, base_branch=base_branch, strategy=strategy
        )

    def get_worktree_status(self, worktree_path: str | Path) -> WorktreeStatus | None:
        return _status.get_worktree_status(self, worktree_path)

    def list_worktrees(self) -> list[WorktreeInfo]:
        return _status.list_worktrees(self)

    def prune_worktrees(self) -> GitOperationResult:
        return _status.prune_worktrees(self)

    def lock_worktree(
        self,
        worktree_path: str | Path,
        reason: str | None = None,
    ) -> GitOperationResult:
        return _locking.lock_worktree(self, worktree_path, reason=reason)

    def unlock_worktree(self, worktree_path: str | Path) -> GitOperationResult:
        return _locking.unlock_worktree(self, worktree_path)

    def get_default_branch(self) -> str:
        return _branch.get_default_branch(self)

    def get_current_branch(self) -> str | None:
        return _branch.get_current_branch(self)

    def has_unpushed_commits(self, branch: str | None = None) -> tuple[bool, int]:
        return _branch.has_unpushed_commits(self, branch=branch)

    def get_local_commit(self, branch: str) -> str | None:
        return _branch.get_local_commit(self, branch)

    def merge_branch(
        self,
        source_branch: str,
        target_branch: str = "main",
        push: bool = True,
    ) -> GitOperationResult:
        return _merge.merge_branch(self, source_branch, target_branch=target_branch, push=push)

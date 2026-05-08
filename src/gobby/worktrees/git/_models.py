"""Dataclasses returned by git worktree operations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WorktreeInfo:
    """Information about a git worktree."""

    path: str
    branch: str | None
    commit: str
    is_bare: bool = False
    is_detached: bool = False
    locked: bool = False
    prunable: bool = False


@dataclass
class WorktreeStatus:
    """Status of a worktree including changes and sync state."""

    has_uncommitted_changes: bool
    has_staged_changes: bool
    has_untracked_files: bool
    ahead: int  # Commits ahead of upstream
    behind: int  # Commits behind upstream
    branch: str | None
    commit: str | None


@dataclass
class GitOperationResult:
    """Result of a git operation."""

    success: bool
    message: str
    output: str | None = None
    error: str | None = None

"""Shared build workspace types and errors."""

from __future__ import annotations

from typing import Literal

WorkspaceBackend = Literal["worktree", "clone"]


class BuildWorkspaceError(ValueError):
    """Raised when build integration workspace state is unsafe to reuse."""

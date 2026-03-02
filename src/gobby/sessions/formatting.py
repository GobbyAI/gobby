"""Session formatting helpers.

Functions for rendering session handoff context as markdown.
Relocated from workflows/context_actions.py as part of dead-code cleanup.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class HandoffContext(Protocol):
    active_gobby_task: dict[str, Any] | None
    active_worktree: dict[str, Any] | None
    git_commits: list[dict[str, Any]]
    git_status: str
    files_modified: list[str]
    initial_goal: str
    recent_activity: list[str]


def format_handoff_as_markdown(
    ctx: HandoffContext, prompt_template: str | None = None, cwd: Path | None = None
) -> str:
    """Format HandoffContext as markdown for storage.

    Args:
        ctx: HandoffContext with extracted session data
        prompt_template: Optional custom template (unused, reserved for future)

    Returns:
        Formatted markdown string with all sections
    """
    _ = prompt_template  # Reserved for future template support
    sections: list[str] = []

    # Active task section
    if ctx.active_gobby_task:
        task = ctx.active_gobby_task
        sections.append(
            f"### Active Task\n"
            f"**{task.get('title', 'Untitled')}** ({task.get('id', 'unknown')})\n"
            f"Status: {task.get('status', 'unknown')}"
        )

    # Worktree context section
    if ctx.active_worktree:
        wt = ctx.active_worktree
        lines = ["### Worktree Context"]
        lines.append(f"- **Branch**: `{wt.get('branch_name', 'unknown')}`")
        lines.append(f"- **Path**: `{wt.get('worktree_path', 'unknown')}`")
        lines.append(f"- **Base**: `{wt.get('base_branch', 'main')}`")
        if wt.get("task_id"):
            lines.append(f"- **Task**: {wt.get('task_id')}")
        sections.append("\n".join(lines))

    # Git commits section
    if ctx.git_commits:
        lines = ["### Commits This Session"]
        for commit in ctx.git_commits:
            lines.append(f"- `{commit.get('hash', '')[:7]}` {commit.get('message', '')}")
        sections.append("\n".join(lines))

    # Git status section
    if ctx.git_status:
        sections.append(f"### Uncommitted Changes\n```\n{ctx.git_status}\n```")

    # Files modified section - only show files still dirty (not yet committed)
    if ctx.files_modified and ctx.git_status:
        # Filter to files that appear in git status (still uncommitted)
        # Normalize paths: files_modified may have absolute paths, git_status has relative
        cwd = cwd or Path.cwd()
        dirty_files = []
        for f in ctx.files_modified:
            # Try to make path relative to cwd for comparison
            try:
                rel_path = Path(f).relative_to(cwd)
                rel_str = str(rel_path)
            except ValueError:
                # Path not relative to cwd, use as-is
                rel_str = f
            if re.search(rf"\b{re.escape(rel_str)}$", ctx.git_status, re.MULTILINE):
                dirty_files.append(rel_str)
        if dirty_files:
            lines = ["### Files Being Modified"]
            for f in dirty_files:
                lines.append(f"- {f}")
            sections.append("\n".join(lines))

    # Initial goal section - only if task is still active (not closed/completed)
    if ctx.initial_goal:
        task_status = None
        if ctx.active_gobby_task:
            task_status = ctx.active_gobby_task.get("status")
        # Only include if no task or task is still open/in_progress.
        # Note: None, "open", and "in_progress" are considered active states.
        if task_status in (None, "open", "in_progress"):
            sections.append(f"### Original Goal\n{ctx.initial_goal}")

    # Recent activity section
    if ctx.recent_activity:
        lines = ["### Recent Activity"]
        for activity in ctx.recent_activity[-5:]:
            lines.append(f"- {activity}")
        sections.append("\n".join(lines))

    # Note: Active Skills section removed - redundant with _build_skill_injection_context()
    # which already handles skill restoration on session start

    return "\n\n".join(sections)

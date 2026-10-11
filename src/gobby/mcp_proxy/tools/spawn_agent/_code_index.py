"""Code-index helpers for isolated spawn_agent runs."""

from __future__ import annotations


def code_index_preflight_mode(
    *,
    checkout_mode: str,
    task_category: str | None,
) -> str | None:
    if checkout_mode in {"worktree", "clone"} and task_category != "docs":
        return "best_effort"
    return None

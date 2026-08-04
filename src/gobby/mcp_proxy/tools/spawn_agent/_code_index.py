"""Code-index helpers for isolated spawn_agent runs."""

from __future__ import annotations

from typing import Any

_PLANNING_CODE_INDEX_AGENTS = frozenset({"planner", "plan-adversary", "plan-enhancer"})


def code_index_preflight_mode(
    *,
    isolation: str,
    agent_name: str | None,
    initial_variables: dict[str, Any] | None,
    task_category: str | None,
) -> str | None:
    if _requires_planning_code_index(agent_name, initial_variables):
        return "required"
    if isolation in {"worktree", "clone"} and task_category != "docs":
        return "best_effort"
    return None


def _requires_planning_code_index(
    agent_name: str | None,
    initial_variables: dict[str, Any] | None,
) -> bool:
    if agent_name not in _PLANNING_CODE_INDEX_AGENTS:
        return False
    stage_name = (initial_variables or {}).get("stage_name")
    return stage_name == "planning"

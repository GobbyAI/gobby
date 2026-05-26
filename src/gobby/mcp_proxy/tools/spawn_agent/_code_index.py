"""Code-index helpers for isolated spawn_agent runs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from gobby.agents.isolation import ensure_isolation_code_index

logger = logging.getLogger(__name__)

_PLANNING_CODE_INDEX_AGENTS = frozenset({"planner", "plan-adversary"})


@dataclass(frozen=True)
class CodeIndexSpawnPreparation:
    warning: dict[str, str] | None = None
    env: dict[str, str] | None = None
    error: str | None = None


async def prepare_isolation_code_index(
    cwd: str,
    daemon_config: Any | None,
) -> tuple[dict[str, str] | None, dict[str, str]]:
    """Run code-index preflight and return a warning plus child env additions."""
    try:
        preflight = await ensure_isolation_code_index(
            cwd,
            database_url=getattr(daemon_config, "database_url", None),
            daemon_bind_host=getattr(daemon_config, "bind_host", None),
            daemon_port=getattr(daemon_config, "daemon_port", None),
        )
        env = getattr(preflight, "env", {})
        return None, env if isinstance(env, dict) else {}
    except Exception as exc:
        reason = str(exc)
        log = logger.info if reason.startswith("gcode_index_timeout:") else logger.warning
        log(
            "Continuing isolated spawn after code index preflight failed for cwd=%s: %s",
            cwd,
            reason,
        )
        return {
            "preflight": "code_index",
            "cwd": cwd,
            "message": reason,
        }, {}


async def prepare_spawn_code_index(
    *,
    cwd: str,
    daemon_config: Any | None,
    isolation: str,
    agent_name: str | None,
    initial_variables: dict[str, Any] | None,
    task_category: str | None,
) -> CodeIndexSpawnPreparation:
    """Prepare code-index access for a spawn, returning env additions or spawn failure."""
    if _requires_planning_code_index(agent_name, initial_variables):
        try:
            preflight = await ensure_isolation_code_index(
                cwd,
                database_url=getattr(daemon_config, "database_url", None),
                daemon_bind_host=getattr(daemon_config, "bind_host", None),
                daemon_port=getattr(daemon_config, "daemon_port", None),
            )
        except Exception as exc:
            reason = str(exc)
            logger.warning(
                "Blocking planning spawn after code index preflight failed for cwd=%s: %s",
                cwd,
                reason,
            )
            return CodeIndexSpawnPreparation(error=f"planner_code_index_unavailable:{reason}")
        env = getattr(preflight, "env", {})
        return CodeIndexSpawnPreparation(env=env if isinstance(env, dict) else {})

    if isolation in {"worktree", "clone"} and task_category != "docs":
        warning, env = await prepare_isolation_code_index(cwd, daemon_config)
        return CodeIndexSpawnPreparation(warning=warning, env=env)

    return CodeIndexSpawnPreparation(env={})


def _requires_planning_code_index(
    agent_name: str | None,
    initial_variables: dict[str, Any] | None,
) -> bool:
    if agent_name not in _PLANNING_CODE_INDEX_AGENTS:
        return False
    stage_name = (initial_variables or {}).get("stage_name")
    return stage_name == "planning"


def without_code_index_skill(skills: list[str]) -> list[str]:
    return [skill for skill in skills if skill != "code-index"]


def append_code_index_warning(prompt: str, warning: dict[str, str]) -> str:
    message = warning.get("message", "unknown")
    return (
        f"{prompt}\n\n---\n\n"
        "## Code Index\n"
        "Use standard file search and read tools for code navigation in this isolated "
        f"workspace. Code-index preflight failed: {message}"
    )

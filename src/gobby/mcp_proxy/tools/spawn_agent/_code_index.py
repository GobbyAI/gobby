"""Code-index helpers for isolated spawn_agent runs."""

from __future__ import annotations

import logging
from typing import Any

from gobby.agents.isolation import ensure_isolation_code_index

logger = logging.getLogger(__name__)


async def prepare_isolation_code_index(
    cwd: str,
    daemon_config: Any | None,
) -> tuple[dict[str, str] | None, dict[str, str]]:
    """Run code-index preflight and return a warning plus child env additions."""
    try:
        preflight = await ensure_isolation_code_index(
            cwd,
            database_url=getattr(daemon_config, "database_url", None),
        )
        env = getattr(preflight, "env", {})
        return None, env if isinstance(env, dict) else {}
    except Exception as exc:
        reason = str(exc)
        logger.warning(
            "Continuing isolated spawn after code index preflight failed for cwd=%s: %s",
            cwd,
            reason,
        )
        return {
            "preflight": "code_index",
            "cwd": cwd,
            "message": reason,
        }, {}


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

"""Deterministic persisted session-title helpers."""

from __future__ import annotations

from typing import Any

PROVISIONAL_TITLE_SOURCE = "provisional"
HEURISTIC_TITLE_SOURCE = "heuristic"
TASK_TITLE_SOURCE = "task"
MANUAL_TITLE_SOURCE = "manual"

_PROVIDER_TITLE_LABELS = {
    "agy": "AGY",
    "claude": "Claude",
    "claude_code": "Claude Code",
    "codex": "Codex",
    "droid": "Droid",
    "grok": "Grok",
    "pipeline": "Pipeline",
    "qwen": "Qwen",
    "system": "System",
    "unknown": "Unknown",
}


def manual_title_source(title: object) -> str | None:
    """Return the manual source marker for a nonblank explicit title."""
    return MANUAL_TITLE_SOURCE if isinstance(title, str) and title.strip() else None


def project_name_for_session_title(executor: Any, project_id: str) -> str:
    """Return the authoritative project name used in automatic session titles."""
    row = executor.execute(
        "SELECT name FROM projects WHERE id = %s",
        (project_id,),
    ).fetchone()
    if row is None or not str(row["name"] or "").strip():
        return project_id
    return str(row["name"]).strip()


def provider_title_label(source: str) -> str:
    """Return a concise display label for a session provider source."""
    normalized = source.strip()
    if not normalized:
        return "Unknown"
    return _PROVIDER_TITLE_LABELS.get(normalized.lower(), normalized)


def format_provisional_session_title(
    project_name: str,
    session_seq_num: int,
    source: str,
) -> str:
    """Return the deterministic title for a session without an open claim."""
    del source
    return f"{project_name.strip()}#{session_seq_num}"


def format_heuristic_session_title(
    project_name: str,
    session_seq_num: int,
    heuristic: str,
) -> str:
    """Return the deterministic display title for a prompt heuristic."""
    return f"{project_name.strip()}#{session_seq_num}: {heuristic.strip()}"


def format_task_session_title(
    project_name: str,
    session_seq_num: int,
    task_seq_num: int,
    title: str,
) -> str:
    """Return the deterministic title for a successfully claimed task."""
    return f"{project_name.strip()}#{session_seq_num}: Task #{task_seq_num} - {title.strip()}"

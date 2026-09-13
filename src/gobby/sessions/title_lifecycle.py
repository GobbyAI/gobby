"""Deterministic session-title transitions driven by task claims."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from gobby.storage.sessions._title_defaults import (
    HEURISTIC_TITLE_SOURCE,
    MANUAL_TITLE_SOURCE,
    PROVISIONAL_TITLE_SOURCE,
    TASK_TITLE_SOURCE,
    format_heuristic_session_title,
    format_provisional_session_title,
    format_task_session_title,
    project_name_for_session_title,
)

_HEURISTIC_WORD_RE = re.compile(r"[^\W_]+(?:['’\u2010-\u2015-][^\W_]+)*", re.UNICODE)
_HEURISTIC_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "can",
        "could",
        "for",
        "from",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "please",
        "the",
        "this",
        "to",
        "we",
        "with",
        "would",
        "you",
    }
)
_HEURISTIC_SUFFIX_LIMIT = 60


@dataclass(frozen=True, slots=True)
class ClaimedTaskTitle:
    seq_num: int
    title: str


@dataclass(frozen=True, slots=True)
class SessionTitleContext:
    project_name: str
    session_seq_num: int
    source: str


def _session_title_context(executor: Any, session: Any) -> SessionTitleContext | None:
    session_seq_num = getattr(session, "seq_num", None)
    project_id = getattr(session, "project_id", None)
    source = getattr(session, "source", None)
    if (
        not isinstance(session_seq_num, int)
        or not isinstance(project_id, str)
        or not isinstance(source, str)
    ):
        return None
    return SessionTitleContext(
        project_name=project_name_for_session_title(executor, project_id),
        session_seq_num=session_seq_num,
        source=source,
    )


def latest_open_claimed_task(
    db: Any, session_id: str, *, conn: Any = None
) -> ClaimedTaskTitle | None:
    """Return the latest still-open task currently claimed by a session."""
    executor = conn if conn is not None else db
    row = executor.execute(
        """
        SELECT seq_num, title
        FROM tasks
        WHERE claimed_by_session_id = %s
          AND closed_at IS NULL
        ORDER BY updated_at DESC, seq_num DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    if row is None or not isinstance(row["seq_num"], int):
        return None
    title = str(row["title"] or "").strip()
    return ClaimedTaskTitle(row["seq_num"], title) if title else None


def heuristic_title_suffix(prompt: str) -> str | None:
    """Build a concise title suffix from the first useful prompt words."""
    stripped = prompt.strip()
    if not stripped or stripped.startswith("/") or stripped.startswith("mcp__"):
        return None
    words = [
        word
        for word in _HEURISTIC_WORD_RE.findall(stripped)
        if any(character.isalpha() for character in word)
        and word.casefold() not in _HEURISTIC_STOPWORDS
    ][:4]
    if not words:
        return None
    suffix = " ".join(words)
    if len(suffix) > _HEURISTIC_SUFFIX_LIMIT:
        suffix = f"{suffix[: _HEURISTIC_SUFFIX_LIMIT - 1].rstrip()}…"
    return suffix


def promote_heuristic_title(session_manager: Any, session_id: str, prompt: str) -> Any:
    """Persist the first prompt heuristic and display it when precedence permits."""
    suffix = heuristic_title_suffix(prompt)
    session = session_manager.get(session_id)
    if suffix is None or session is None:
        return session
    context = _session_title_context(session_manager.db, session)
    if context is None:
        return session
    candidate = format_heuristic_session_title(
        context.project_name,
        context.session_seq_num,
        suffix,
    )
    with session_manager.db.transaction() as conn:
        row = conn.execute(
            """
            UPDATE sessions
            SET heuristic_title = COALESCE(heuristic_title, %s)
            WHERE id = %s
            RETURNING heuristic_title
            """,
            (candidate, session_id),
        ).fetchone()
    if row is None or not isinstance(row["heuristic_title"], str):
        return session_manager.get(session_id)
    return session_manager.update_title(
        session_id,
        row["heuristic_title"],
        title_source=HEURISTIC_TITLE_SOURCE,
    )


def update_title_for_claim(session_manager: Any, session_id: str, task: Any) -> Any:
    """Apply the deterministic task title after a successful claim."""
    task_seq_num = getattr(task, "seq_num", None)
    title = getattr(task, "title", None)
    session = session_manager.get(session_id)
    if (
        session is None
        or getattr(session, "title_source", None) == MANUAL_TITLE_SOURCE
        or not isinstance(task_seq_num, int)
        or not isinstance(title, str)
        or not title.strip()
    ):
        return session
    context = _session_title_context(session_manager.db, session)
    if context is None:
        return session
    return session_manager.update_title(
        session_id,
        format_task_session_title(
            context.project_name,
            context.session_seq_num,
            task_seq_num,
            title,
        ),
        title_source=TASK_TITLE_SOURCE,
    )


def recompute_automatic_title(session_manager: Any, session_id: str) -> Any:
    """Recompute a nonmanual title from the latest open claim or provisional rule."""
    session = session_manager.get(session_id)
    if session is None or getattr(session, "title_source", None) == MANUAL_TITLE_SOURCE:
        return session
    context = _session_title_context(session_manager.db, session)
    if context is None:
        return session
    task = latest_open_claimed_task(session_manager.db, session_id)
    if task is not None:
        title = format_task_session_title(
            context.project_name,
            context.session_seq_num,
            task.seq_num,
            task.title,
        )
        source = TASK_TITLE_SOURCE
    elif isinstance(getattr(session, "heuristic_title", None), str):
        title = session.heuristic_title
        source = HEURISTIC_TITLE_SOURCE
    else:
        title = format_provisional_session_title(
            context.project_name,
            context.session_seq_num,
            context.source,
        )
        source = PROVISIONAL_TITLE_SOURCE
    return session_manager.update_title(
        session_id,
        title,
        title_source=source,
        allow_task_fallback=True,
    )


def clear_successor_title(conn: Any, predecessor: Any, successor_seq_num: int) -> tuple[str, str]:
    """Choose the title persisted on a clear successor before claim transfer."""
    title = getattr(predecessor, "title", None)
    if getattr(predecessor, "title_source", None) == MANUAL_TITLE_SOURCE and isinstance(title, str):
        return title, MANUAL_TITLE_SOURCE
    task = latest_open_claimed_task(None, predecessor.id, conn=conn)
    project_id = getattr(predecessor, "project_id", None)
    source = getattr(predecessor, "source", None)
    if not isinstance(project_id, str) or not isinstance(source, str):
        raise ValueError("Clear predecessor is missing session title identity")
    project_name = project_name_for_session_title(conn, project_id)
    if task is not None:
        return (
            format_task_session_title(
                project_name,
                successor_seq_num,
                task.seq_num,
                task.title,
            ),
            TASK_TITLE_SOURCE,
        )
    return (
        format_provisional_session_title(project_name, successor_seq_num, source),
        PROVISIONAL_TITLE_SOURCE,
    )


def apply_clear_successor_title(
    session_manager: Any,
    successor_id: str,
    predecessor: Any,
) -> Any:
    """Inherit a manual title or recompute the successor's automatic title."""
    title = getattr(predecessor, "title", None)
    if getattr(predecessor, "title_source", None) == MANUAL_TITLE_SOURCE and isinstance(title, str):
        return session_manager.update_title(
            successor_id,
            title,
            title_source=MANUAL_TITLE_SOURCE,
        )
    return recompute_automatic_title(session_manager, successor_id)

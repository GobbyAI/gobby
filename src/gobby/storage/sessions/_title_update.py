"""Atomic session-title ownership updates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from gobby.storage.hub.protocol import Transaction

from ._title_defaults import (
    MANUAL_TITLE_SOURCE,
    PROVISIONAL_TITLE_SOURCE,
    TASK_TITLE_SOURCE,
)

TITLE_UPDATE_ALLOWED_SQL = f"""
(
    incoming.title_source = '{MANUAL_TITLE_SOURCE}'
    OR (
        incoming.title_source = '{TASK_TITLE_SOURCE}'
        AND current_session.title_source IS DISTINCT FROM '{MANUAL_TITLE_SOURCE}'
    )
    OR (
        COALESCE(incoming.title_source, '{PROVISIONAL_TITLE_SOURCE}')
            = '{PROVISIONAL_TITLE_SOURCE}'
        AND (
            NULLIF(BTRIM(current_session.title), '') IS NULL
            OR current_session.title_source IS NULL
            OR current_session.title_source = '{PROVISIONAL_TITLE_SOURCE}'
        )
    )
)
""".strip()


@dataclass(frozen=True)
class TitleMutationResult:
    """Outcome of an attempted title/title-source pair mutation."""

    applied: bool
    title_changed: bool


def apply_title_mutation(
    conn: Transaction,
    session_id: str,
    *,
    title_is_set: bool,
    title: str | None,
    title_source_is_set: bool,
    title_source: str | None,
    updated_at: datetime,
) -> TitleMutationResult | None:
    """Apply a title pair mutation according to persisted ownership."""
    if not title_is_set and not title_source_is_set:
        return None

    row = conn.execute(
        """
        SELECT title, title_source
        FROM sessions
        WHERE id = %s
        FOR UPDATE
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return TitleMutationResult(applied=False, title_changed=False)

    current_title = _optional_str(row["title"])
    current_title_source = _optional_str(row["title_source"])
    desired_title = title if title_is_set else current_title
    desired_title_source = title_source if title_source_is_set else current_title_source

    cursor = conn.execute(
        f"""
        UPDATE sessions AS current_session
        SET title = incoming.title,
            title_source = incoming.title_source,
            updated_at = %s
        FROM (
            VALUES (%s::text, %s::text)
        ) AS incoming(title, title_source)
        WHERE current_session.id = %s
          AND {TITLE_UPDATE_ALLOWED_SQL}
          AND (
              current_session.title IS DISTINCT FROM incoming.title
              OR current_session.title_source IS DISTINCT FROM incoming.title_source
          )
        """,  # nosec B608
        (updated_at, desired_title, desired_title_source, session_id),
    )
    applied = bool(cursor.rowcount and cursor.rowcount > 0)
    return TitleMutationResult(
        applied=applied,
        title_changed=applied and current_title != desired_title,
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None

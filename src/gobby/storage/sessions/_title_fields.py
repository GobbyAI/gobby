"""Session title field updates and side effects."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar, Protocol

from gobby.storage.session_models import Session
from gobby.utils.datetime import utc_now

from ._bootstrap import TitleChangeCallback
from ._constants import get_logger
from ._title_defaults import MANUAL_TITLE_SOURCE
from ._title_update import apply_title_mutation

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


class _TitleFieldHost(Protocol):
    db: HubDatabase
    _title_listeners: list[TitleChangeCallback]
    _VALID_TITLE_SOURCES: ClassVar[set[str]]

    def get(self, session_id: str) -> Session | None: ...

    def _notify_session_change(self, event: str, session_id: str) -> None: ...

    def _run_title_change_side_effects(self, updated: Session, title: str) -> None: ...


class _TitleFieldMixin:
    def normalize_automatic_title_refs(self: _TitleFieldHost) -> int:
        """Refresh automatic prefixes without changing suffixes or provenance."""
        rows = self.db.fetchall(
            """
            SELECT s.id, s.title, s.title_source, s.heuristic_title, s.seq_num, s.project_id,
                   p.name AS project_name
            FROM sessions s LEFT JOIN projects p ON p.id = s.project_id
            WHERE (s.title_source IN ('provisional', 'heuristic', 'task')
                   OR s.heuristic_title IS NOT NULL)
              AND s.seq_num IS NOT NULL
            """
        )
        changed = 0
        for row in rows:
            title = row["title"] or ""
            project = str(row["project_name"] or "").strip() or str(row["project_id"])

            def normalize(
                value: str,
                project_name: str = project,
                session_seq_num: object = row["seq_num"],
            ) -> str:
                prefix = re.match(r"^(?:\([^)]*#\d+\)|[^\s:()]+#\d+)(?=:|$)", value)
                return (
                    f"{project_name}#{session_seq_num}" + value[prefix.end() :]
                    if prefix is not None
                    else value
                )

            normalized = normalize(title)
            heuristic = str(row["heuristic_title"] or "")
            normalized_heuristic = normalize(heuristic) or None
            if normalized == title and normalized_heuristic == (heuristic or None):
                continue
            with self.db.transaction() as conn:
                cursor = conn.execute(
                    """
                    UPDATE sessions
                    SET title = %s, heuristic_title = %s, updated_at = %s
                    WHERE id = %s AND title IS NOT DISTINCT FROM %s
                      AND title_source IS NOT DISTINCT FROM %s
                      AND heuristic_title IS NOT DISTINCT FROM %s
                    """,
                    (
                        normalized,
                        normalized_heuristic,
                        utc_now(),
                        row["id"],
                        row["title"],
                        row["title_source"],
                        row["heuristic_title"],
                    ),
                )
                applied = bool(cursor.rowcount)
            if applied:
                changed += 1
                updated = self.get(str(row["id"]))
                if updated is not None:
                    self._notify_session_change("session_updated", updated.id)
                    self._run_title_change_side_effects(updated, updated.title or "")
        return changed

    def update_title(
        self: _TitleFieldHost,
        session_id: str,
        title: str,
        *,
        title_source: str | None = MANUAL_TITLE_SOURCE,
        allow_task_fallback: bool = False,
    ) -> Session | None:
        """Update session title."""
        current = self.get(session_id)
        if current is None:
            return None
        if title_source is not None and title_source not in self._VALID_TITLE_SOURCES:
            raise ValueError(
                f"Invalid title_source {title_source!r}. Must be one of: "
                f"{', '.join(sorted(self._VALID_TITLE_SOURCES))}"
            )

        now = utc_now()
        with self.db.transaction() as conn:
            mutation = apply_title_mutation(
                conn,
                session_id,
                title_is_set=True,
                title=title,
                title_source_is_set=title_source is not None,
                title_source=title_source,
                allow_task_fallback=allow_task_fallback,
                updated_at=now,
            )
        updated = self.get(session_id)
        if updated is None:
            return None
        if mutation is None or not mutation.applied:
            return updated

        self._notify_session_change("session_updated", session_id)
        if mutation.title_changed:
            self._run_title_change_side_effects(updated, updated.title or "")
        return updated

    def _run_title_change_side_effects(
        self: _TitleFieldHost,
        updated: Session,
        title: str,
    ) -> None:
        session_id = updated.id
        try:
            from gobby.sessions.tmux_window_naming import schedule_tmux_window_rename

            schedule_tmux_window_rename(updated, title)
        except Exception:
            get_logger().warning(
                "Failed to schedule tmux title update for session %s",
                session_id,
                exc_info=True,
            )

        for listener in list(self._title_listeners):
            try:
                listener(session_id, title)
            except Exception:
                get_logger().warning(
                    "Title listener failed for session %s", session_id, exc_info=True
                )

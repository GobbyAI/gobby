"""Session title field updates and side effects."""

from __future__ import annotations

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
    def update_title(
        self: _TitleFieldHost,
        session_id: str,
        title: str,
        *,
        title_source: str | None = MANUAL_TITLE_SOURCE,
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

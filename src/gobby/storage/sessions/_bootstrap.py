"""Bootstrap mixins for session storage."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

type TitleChangeCallback = Callable[[str, str], None]
type SessionChangeCallback = Callable[[str, str], None]


class _SessionBootstrapHost(Protocol):
    _title_listeners: list[TitleChangeCallback]
    _session_change_listeners: list[SessionChangeCallback]
    db: Any


class _SessionBootstrapMixin:
    def register_title_listener(self: _SessionBootstrapHost, listener: TitleChangeCallback) -> None:
        """Register a sync callback fired after successful title changes."""
        self._title_listeners.append(listener)

    def unregister_title_listener(
        self: _SessionBootstrapHost, listener: TitleChangeCallback
    ) -> None:
        """Remove a previously-registered title listener if present."""
        try:
            self._title_listeners.remove(listener)
        except ValueError:
            return

    def register_session_change_listener(
        self: _SessionBootstrapHost, listener: SessionChangeCallback
    ) -> None:
        """Register a sync callback fired after successful session mutations."""
        self._session_change_listeners.append(listener)

    def unregister_session_change_listener(
        self: _SessionBootstrapHost, listener: SessionChangeCallback
    ) -> None:
        """Remove a previously-registered session change listener if present."""
        try:
            self._session_change_listeners.remove(listener)
        except ValueError:
            return

    def _notify_session_change(
        self: _SessionBootstrapHost,
        event: str,
        session_id: str,
    ) -> None:
        """Notify listeners after the surrounding DB transaction commits."""

        def _run() -> None:
            from ._constants import get_logger

            for listener in list(self._session_change_listeners):
                try:
                    listener(event, session_id)
                except Exception:
                    get_logger().warning(
                        "Session change listener failed for %s (%s)",
                        session_id,
                        event,
                        exc_info=True,
                    )

        after_commit = getattr(self.db, "after_commit", None)
        if callable(after_commit):
            after_commit(_run)
            return
        _run()

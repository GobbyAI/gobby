"""Bootstrap mixins for session storage."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class _SessionBootstrapHost(Protocol):
    _title_listeners: list[Callable[[str, str], None]]


class _SessionBootstrapMixin:
    def register_title_listener(
        self: _SessionBootstrapHost, listener: Callable[[str, str], None]
    ) -> None:
        """Register a sync callback fired after successful title changes."""
        self._title_listeners.append(listener)

    def unregister_title_listener(
        self: _SessionBootstrapHost, listener: Callable[[str, str], None]
    ) -> None:
        """Remove a previously-registered title listener if present."""
        try:
            self._title_listeners.remove(listener)
        except ValueError:
            return

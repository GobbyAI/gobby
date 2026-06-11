"""Worktree lifecycle event helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

WorktreeEvent = dict[str, Any]
WorktreeEventListener = Callable[[WorktreeEvent], None]

_listeners: list[WorktreeEventListener] = []


def add_worktree_event_listener(listener: WorktreeEventListener) -> Callable[[], None]:
    """Register a process-local listener for daemon worktree lifecycle events."""
    _listeners.append(listener)

    def remove() -> None:
        try:
            _listeners.remove(listener)
        except ValueError:
            pass

    return remove


def emit_worktree_event(event_type: str, **payload: Any) -> WorktreeEvent:
    """Emit a daemon-local worktree lifecycle event."""
    event: WorktreeEvent = {"event_type": event_type, **payload}
    logger.info("Worktree lifecycle event", extra={"worktree_event": event})

    for listener in tuple(_listeners):
        try:
            listener(dict(event))
        except Exception:
            logger.warning("Worktree lifecycle event listener failed", exc_info=True)

    return event

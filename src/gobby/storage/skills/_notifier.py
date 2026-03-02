"""Skill change notification system."""

import logging
from collections.abc import Callable
from typing import Any

from gobby.storage.skills._models import ChangeEvent, ChangeEventType

logger = logging.getLogger(__name__)

ChangeListener = Callable[[ChangeEvent], None]


class SkillChangeNotifier:
    """Notifies registered listeners when skills are mutated.

    This implements the observer pattern to allow components like
    search indexes to stay synchronized with skill changes.

    Listeners are wrapped in try/except to prevent one failing listener
    from blocking others or the main mutation.

    Example usage:
        ```python
        notifier = SkillChangeNotifier()

        def on_skill_change(event: ChangeEvent):
            print(f"Skill {event.skill_name} was {event.event_type}d")

        notifier.add_listener(on_skill_change)

        manager = LocalSkillManager(db, notifier=notifier)
        manager.create_skill(...)  # Triggers the listener
        ```
    """

    def __init__(self) -> None:
        """Initialize the notifier with an empty listener list."""
        self._listeners: list[ChangeListener] = []

    def add_listener(self, listener: ChangeListener) -> None:
        """Register a listener to receive change events.

        Args:
            listener: Callable that accepts a ChangeEvent
        """
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: ChangeListener) -> bool:
        """Unregister a listener.

        Args:
            listener: The listener to remove

        Returns:
            True if removed, False if not found
        """
        try:
            self._listeners.remove(listener)
            return True
        except ValueError:
            return False

    def fire_change(
        self,
        event_type: ChangeEventType,
        skill_id: str,
        skill_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Fire a change event to all registered listeners.

        Each listener is called in a try/except block to prevent
        one failing listener from blocking others.

        Args:
            event_type: Type of change ('create', 'update', 'delete')
            skill_id: ID of the affected skill
            skill_name: Name of the affected skill
            metadata: Optional additional context
        """
        event = ChangeEvent(
            event_type=event_type,
            skill_id=skill_id,
            skill_name=skill_name,
            metadata=metadata,
        )

        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception as e:
                logger.error(
                    "Error in skill change listener %s: %s",
                    listener,
                    e,
                    exc_info=True,
                )

    def clear_listeners(self) -> None:
        """Remove all registered listeners."""
        self._listeners.clear()

    @property
    def listener_count(self) -> int:
        """Return the number of registered listeners."""
        return len(self._listeners)


"""Handler registration and EventHandlers initialization tests."""

from __future__ import annotations

import pytest

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType

pytestmark = pytest.mark.unit


class TestHandlerRegistration:
    """Test handler registration and lookup."""

    def test_all_event_types_have_handlers(self, event_handlers: EventHandlers) -> None:
        """Test that all HookEventType values have registered handlers."""
        for event_type in HookEventType:
            handler = event_handlers.get_handler(event_type)
            assert handler is not None, f"No handler for {event_type}"
            assert callable(handler)

    def test_get_handler_returns_callable(self, event_handlers: EventHandlers) -> None:
        """Test get_handler returns a callable."""
        handler = event_handlers.get_handler(HookEventType.SESSION_START)
        assert callable(handler)

    def test_get_handler_for_unknown_returns_none(self, event_handlers: EventHandlers) -> None:
        """Test get_handler returns None for unknown event type."""
        result = event_handlers.get_handler("invalid_event")  # type: ignore
        assert result is None

    def test_handler_map_is_immutable(self, event_handlers: EventHandlers) -> None:
        """Test handler map cannot be modified externally."""
        handler_map = event_handlers.get_handler_map()
        original_count = len(handler_map)
        handler_map["fake"] = lambda x: x
        assert len(event_handlers.get_handler_map()) == original_count


class TestEventHandlersInit:
    """Test EventHandlers initialization."""

    def test_init_creates_logger(self) -> None:
        """Test init creates logger if not provided."""
        handlers = EventHandlers()
        assert handlers.logger is not None

    def test_init_with_dependencies(self, mock_dependencies: dict) -> None:
        """Test init with dependencies."""
        handlers = EventHandlers(**mock_dependencies)
        assert handlers._session_manager is mock_dependencies["session_manager"]

    def test_init_default_get_machine_id(self) -> None:
        """Test default get_machine_id function returns unknown-machine."""
        handlers = EventHandlers()
        assert handlers._get_machine_id() == "unknown-machine"

    def test_init_default_resolve_project_id(self) -> None:
        """Test default resolve_project_id function returns project_id or empty string."""
        handlers = EventHandlers()
        assert handlers._resolve_project_id("proj-123", None) == "proj-123"
        assert handlers._resolve_project_id(None, "/some/path") == ""

    def test_init_custom_get_machine_id(self) -> None:
        """Test custom get_machine_id function is used."""
        handlers = EventHandlers(get_machine_id=lambda: "custom-machine")
        assert handlers._get_machine_id() == "custom-machine"

    def test_init_custom_resolve_project_id(self) -> None:
        """Test custom resolve_project_id function is used."""
        handlers = EventHandlers(resolve_project_id=lambda p, c: f"resolved-{p or 'none'}")
        assert handlers._resolve_project_id("proj-1", None) == "resolved-proj-1"

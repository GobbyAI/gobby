"""
Gobby hooks package for Claude Code, Gemini CLI, and Codex integration.

This package provides a hook system for intercepting and processing events
from AI coding assistants. The architecture follows the Coordinator pattern:

Core Components:
    HookManager: Main entry point and coordinator. Receives hook events and
        delegates to specialized components.

    EventHandlers: Contains all event handler implementations for the 15
        supported event types (session, agent, tool, etc.)

    SessionCoordinator: Manages session lifecycle - registration, lookup,
        status tracking, and cleanup.

    HealthMonitor: Background daemon health check monitoring with caching.

    WebhookDispatcher: Dispatches hook events to external webhook endpoints.

Event Models:
    HookEventType: Unified event type enum (15 types across all CLIs)
    SessionSource: Enum identifying which CLI originated the session
    HookEvent: Unified event dataclass from any CLI source
    HookResponse: Unified response dataclass returned to CLIs

Example:
    ```python
    from gobby.hooks import HookManager, HookEvent, HookEventType

    # Create manager (typically done once in daemon)
    manager = HookManager()

    # Handle incoming events
    response = manager.handle(event)
    ```
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.hooks.event_handlers import EventHandlers as EventHandlers
    from gobby.hooks.events import EVENT_TYPE_CLI_SUPPORT as EVENT_TYPE_CLI_SUPPORT
    from gobby.hooks.events import HookEvent as HookEvent
    from gobby.hooks.events import HookEventType as HookEventType
    from gobby.hooks.events import HookResponse as HookResponse
    from gobby.hooks.events import SessionSource as SessionSource
    from gobby.hooks.health_monitor import HealthMonitor as HealthMonitor
    from gobby.hooks.hook_manager import HookManager as HookManager
    from gobby.hooks.session_coordinator import SessionCoordinator as SessionCoordinator
    from gobby.hooks.webhooks import WebhookDispatcher as WebhookDispatcher

__all__ = [
    "HookManager",
    "EventHandlers",
    "SessionCoordinator",
    "HealthMonitor",
    "WebhookDispatcher",
    "HookEventType",
    "SessionSource",
    "HookEvent",
    "HookResponse",
    "EVENT_TYPE_CLI_SUPPORT",
]

_EXPORTS = {
    "EVENT_TYPE_CLI_SUPPORT": ("gobby.hooks.events", "EVENT_TYPE_CLI_SUPPORT"),
    "EventHandlers": ("gobby.hooks.event_handlers", "EventHandlers"),
    "HealthMonitor": ("gobby.hooks.health_monitor", "HealthMonitor"),
    "HookEvent": ("gobby.hooks.events", "HookEvent"),
    "HookEventType": ("gobby.hooks.events", "HookEventType"),
    "HookManager": ("gobby.hooks.hook_manager", "HookManager"),
    "HookResponse": ("gobby.hooks.events", "HookResponse"),
    "SessionCoordinator": ("gobby.hooks.session_coordinator", "SessionCoordinator"),
    "SessionSource": ("gobby.hooks.events", "SessionSource"),
    "WebhookDispatcher": ("gobby.hooks.webhooks", "WebhookDispatcher"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value

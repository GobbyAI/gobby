# Gobby Hooks Package

Hook system for intercepting and processing events from AI coding assistants (Claude Code, Gemini CLI, Codex).

## Architecture

The hooks package follows the **Coordinator Pattern** where `HookManager` acts as a thin orchestration layer that delegates work to specialized components.

```
┌─────────────────────────────────────────────────────────────────┐
│                         HookManager                              │
│  (Coordinator - receives events, delegates to components)        │
└─────────────────────────────────────────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  EventHandlers  │  │ SessionCoord.   │  │  HealthMonitor  │
│  (15 handlers)  │  │ (lifecycle)     │  │  (daemon check) │
└─────────────────┘  └─────────────────┘  └─────────────────┘
          │                   │
          ▼                   ▼
┌─────────────────┐  ┌─────────────────┐
│ WebhookDispatch │  │    Plugins      │
│ (HTTP webhooks) │  │ (extensibility) │
└─────────────────┘  └─────────────────┘
```

## Modules

| Module | Purpose |
|--------|---------|
| `hook_manager.py` | Main coordinator - receives events, routes to handlers |
| `event_handlers.py` | Contains all 15 event handler implementations |
| `events.py` | Event types, HookEvent, HookResponse dataclasses |
| `session_coordinator.py` | Session lifecycle - registration, lookup, cleanup |
| `health_monitor.py` | Background daemon health check with caching |
| `webhooks.py` | HTTP webhook dispatch to external endpoints |
| `plugins.py` | Plugin system for custom event handlers |
| `broadcaster.py` | WebSocket event broadcasting |
| `hook_types.py` | Legacy type definitions |

## Event Flow

1. CLI sends event → `HookManager.handle(event)`
2. HookManager enriches event with context (task_id, session_id)
3. `EventHandlers.get_handler(event_type)` returns appropriate handler
4. Handler processes event, returns `HookResponse`
5. Optional: WebhookDispatcher sends to configured endpoints
6. Optional: Plugins receive event for custom processing
7. Response returned to CLI

## Event Types

```python
class HookEventType(str, Enum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    BEFORE_AGENT = "before_agent"      # User prompt submit
    AFTER_AGENT = "after_agent"        # Agent response complete
    BEFORE_TOOL = "before_tool"        # Pre tool execution
    AFTER_TOOL = "after_tool"          # Post tool execution
    STOP = "stop"                      # Agent stop requested
    PRE_COMPACT = "pre_compact"        # Before context compaction
    NOTIFICATION = "notification"      # General notifications
    # ... and more
```

## Testing Hooks in Isolation

The architecture supports dependency injection for easy testing:

```python
from unittest.mock import MagicMock
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, HookResponse

def test_session_start_handler():
    # Create mocked dependencies
    mock_session_manager = MagicMock()
    mock_session_manager.register_session.return_value = "sess-123"

    mock_workflow_handler = MagicMock()
    mock_workflow_handler.handle.return_value = HookResponse(
        decision="allow",
        context="Task context here"
    )

    # Inject dependencies
    handlers = EventHandlers(
        session_manager=mock_session_manager,
        workflow_handler=mock_workflow_handler,
        logger=MagicMock(),
    )

    # Create test event
    event = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="ext-123",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"cwd": "/project"},
    )

    # Test handler directly
    response = handlers.handle_session_start(event)

    assert response.decision == "allow"
    mock_session_manager.register_session.assert_called_once()
```

### Testing HookManager with Mocked Subsystems

```python
from unittest.mock import patch, MagicMock

@pytest.fixture
def mock_hook_manager():
    with (
        patch("gobby.hooks.hook_manager.HubDatabase"),
        patch("gobby.hooks.hook_manager.DaemonClient"),
    ):
        manager = HookManager(log_file="/tmp/test.log")

        # Mock health monitor
        manager._health_monitor.get_cached_status = MagicMock(
            return_value=(True, None, "running", None)
        )

        return manager
```

## Extending with New Event Types

1. Add event type to `HookEventType` enum in `events.py`:

```python
class HookEventType(str, Enum):
    # ... existing types
    MY_NEW_EVENT = "my_new_event"
```

2. Add handler method in `EventHandlers`:

```python
def handle_my_new_event(self, event: HookEvent) -> HookResponse:
    """Handle my new event type."""
    # Process event
    return HookResponse(decision="allow")
```

3. Register handler in `_build_handler_map()`:

```python
def _build_handler_map(self) -> dict[HookEventType, Callable]:
    return {
        # ... existing handlers
        HookEventType.MY_NEW_EVENT: self.handle_my_new_event,
    }
```

4. Update `EVENT_TYPE_CLI_SUPPORT` in `events.py` to indicate which CLIs support it.

## Plugin System

Create custom plugins by subclassing `HookPlugin`:

```python
from gobby.hooks.plugins import HookPlugin, hook_handler

class MyPlugin(HookPlugin):
    name = "my-plugin"

    @hook_handler(HookEventType.SESSION_START)
    def on_session_start(self, event: HookEvent) -> HookResponse | None:
        # Custom processing
        print(f"Session started: {event.session_id}")
        return None  # Don't modify response
```

Place plugins in `~/.gobby/plugins/` or `.gobby/plugins/` and enable in config:

```yaml
hook_extensions:
  plugins:
    enabled: true
    auto_discover: true
```

## Configuration

Key config options in `~/.gobby/config.yaml`:

```yaml
# Webhook endpoints
hook_extensions:
  webhooks:
    enabled: true
    endpoints:
      - name: slack-notify
        url: ${SLACK_WEBHOOK_URL}
        events: [session_end]

  # Plugin system
  plugins:
    enabled: false  # Security: disabled by default
    plugin_dirs:
      - ~/.gobby/plugins
      - .gobby/plugins
```

## Migration Notes

The hooks package was decomposed from a monolithic `hook_manager.py` (1,681 lines) into focused modules using the Strangler Fig pattern:

- `EventHandlers` extracted: Contains all event processing logic
- `SessionCoordinator` extracted: Session lifecycle management
- `HealthMonitor` extracted: Daemon health checking
- `HookManager` remains as thin coordinator (~300 lines)

**Breaking changes:** None. All public APIs preserved through re-exports in `__init__.py`.

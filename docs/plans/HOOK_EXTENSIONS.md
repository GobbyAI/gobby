# Hook Extensions Plan

## Vision

Transform Gobby's hook system from passive observation into an **extensible platform** where developers can customize behavior through WebSocket subscriptions, webhook callbacks, and Python plugins.

Key insight: **Hooks are already the nervous system of Gobby** - every CLI interaction flows through them. By exposing these events to external consumers and allowing custom handlers, we enable infinite customization without modifying core code.

Inspired by:
- [Claude Code Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) - Event-driven customization
- [Zapier Webhooks](https://zapier.com/help/doc/how-get-started-webhooks-zapier) - External service integration
- [pytest plugins](https://docs.pytest.org/en/stable/how-to/writing_plugins.html) - Python plugin architecture

## Current State

Gobby's hook system currently provides:
- **Unified event model**: `HookEvent` dataclass with 14 event types across 3 CLIs
- **Response control**: `HookResponse` with `allow/deny/ask` decisions and context injection
- **Session tracking**: Auto-registration, status updates, summary generation
- **WebSocket infrastructure**: `broadcast()` method exists but unused by hooks

What's missing:
- No way for external services to subscribe to hook events
- No webhook callouts to external URLs
- No plugin system for custom Python handlers

## Proposed Extensions

### 1. WebSocket Event Broadcasting
Real-time event streaming to connected WebSocket clients.

### 2. Config-Driven Webhooks
HTTP callouts to external services on hook events.

### 3. Python Plugin System
Dynamically loaded Python modules that can intercept and modify hook behavior.

---

## Core Design Principles

1. **Fail-open** - Extensions never block hook execution on failure
2. **Async-first** - All extensions run asynchronously to avoid latency
3. **Config-driven** - Enable/disable via `~/.gobby/config.yaml`
4. **Progressive disclosure** - Simple use cases first, advanced later
5. **Security-conscious** - Plugins require explicit enablement

---

## Architecture

### Enhanced Hook Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                      CLI Invocation                              │
│              (Claude Code / Gemini / Codex)                      │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Hook Dispatcher                                │
│              POST /hooks/execute                                 │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                     HookManager.handle()                         │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  │
│  │  Pre-Plugins    │  │  Core Handler   │  │  Post-Plugins   │  │
│  │  (can block)    │→ │  (existing)     │→ │  (observe only) │  │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘  │
└─────────────────────────┬───────────────────────────────────────┘
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
┌─────────────────┐ ┌───────────────┐ ┌─────────────────┐
│   WebSocket     │ │   Webhooks    │ │   CLI Response  │
│   Broadcast     │ │   (async)     │ │                 │
└─────────────────┘ └───────────────┘ └─────────────────┘
```

### New Components

| Component | Location | Purpose |
|-----------|----------|---------|
| `HookEventBroadcaster` | `src/hooks/broadcaster.py` | WebSocket event streaming |
| `WebhookDispatcher` | `src/hooks/webhooks.py` | HTTP callouts to external URLs |
| `PluginLoader` | `src/hooks/plugins.py` | Dynamic Python plugin loading |
| `PluginRegistry` | `src/hooks/plugins.py` | Plugin lifecycle management |

### Configuration Schema

```yaml
# ~/.gobby/config.yaml

hook_extensions:
  # WebSocket broadcasting
  websocket:
    enabled: true
    broadcast_events:
      - session_start
      - session_end
      - before_tool
      - after_tool
    include_payload: true  # Include event.data in broadcast

  # Webhook callouts
  webhooks:
    enabled: false
    endpoints:
      - url: "https://example.com/hook"
        events: ["session_start", "session_end"]
        headers:
          Authorization: "Bearer ${WEBHOOK_TOKEN}"
        timeout: 5
        retry_count: 2
        can_block: false  # If true, webhook can return deny decision

  # Python plugins
  plugins:
    enabled: false
    plugin_dirs:
      - "~/.gobby/plugins"
      - ".gobby/plugins"  # Project-specific
    auto_discover: true
    plugins:
      my_plugin:
        enabled: true
        config:
          custom_key: "value"
```

---

## Feature Details

### 1. WebSocket Event Broadcasting

**Problem**: External tools (dashboards, IDEs, monitoring) can't observe hook events in real-time.

**Solution**: Broadcast hook events to all connected WebSocket clients.

**Event Format**:
```json
{
  "type": "hook_event",
  "event_type": "before_tool",
  "session_id": "sess-abc123",
  "source": "claude",
  "timestamp": "2025-01-15T10:30:00Z",
  "data": {
    "tool_name": "Edit",
    "tool_input": {"file_path": "/src/main.py", "...": "..."}
  },
  "response": {
    "decision": "allow"
  }
}
```

**Integration**:
- `GobbyRunner` passes WebSocket server reference to HTTP server
- `execute_hook` endpoint broadcasts after handler returns
- Configurable event filtering via `broadcast_events` list

**Client Subscription Example**:
```javascript
const ws = new WebSocket('ws://localhost:8766');
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  if (data.type === 'hook_event') {
    console.log(`[${data.event_type}] ${data.session_id}`);
  }
};
```

---

### 2. Config-Driven Webhooks

**Problem**: No way to notify external services (Slack, PagerDuty, custom APIs) of hook events.

**Solution**: HTTP POST to configured URLs on specified events.

**Webhook Payload**:
```json
{
  "event": "before_tool",
  "timestamp": "2025-01-15T10:30:00Z",
  "session_id": "sess-abc123",
  "source": "claude",
  "project_id": "proj-xyz",
  "machine_id": "mac-123",
  "data": {
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf /tmp/test"}
  }
}
```

**Blocking Webhooks** (optional):
```json
// Webhook response that blocks the action
{
  "decision": "deny",
  "reason": "Destructive command blocked by policy server"
}
```

**Retry Logic**:
- Exponential backoff: 1s, 2s, 4s
- Max retries configurable per endpoint
- Failures logged but never block hook execution (unless `can_block: true`)

**Workflow Integration**:
Webhooks become a workflow action type:
```yaml
# In workflow YAML
on_enter:
  - action: webhook
    url: "{{ config.policy_server }}"
    event: "phase_entered"
    can_block: true
```

---

### 3. Python Plugin System

**Problem**: Some customizations require code logic that can't be expressed in config.

**Solution**: Dynamically load Python modules that register hook handlers.

**Plugin Interface**:
```python
# ~/.gobby/plugins/my_policy.py
from gobby.hooks.plugins import HookPlugin, hook_handler
from gobby.hooks.events import HookEvent, HookResponse, HookEventType

class MyPolicyPlugin(HookPlugin):
    """Custom policy enforcement plugin."""

    name = "my_policy"
    version = "1.0.0"

    def on_load(self, config: dict) -> None:
        """Called when plugin is loaded."""
        self.blocked_commands = config.get("blocked_commands", [])

    @hook_handler(HookEventType.BEFORE_TOOL, priority=10)
    def check_bash_command(self, event: HookEvent) -> HookResponse | None:
        """Block dangerous bash commands."""
        if event.data.get("tool_name") != "Bash":
            return None  # Continue to next handler

        command = event.data.get("tool_input", {}).get("command", "")
        for blocked in self.blocked_commands:
            if blocked in command:
                return HookResponse(
                    decision="deny",
                    reason=f"Command contains blocked pattern: {blocked}"
                )
        return None  # Allow

    @hook_handler(HookEventType.SESSION_START)
    def log_session_start(self, event: HookEvent) -> None:
        """Observe-only handler (no return = allow)."""
        self.logger.info(f"Session started: {event.session_id}")
```

**Plugin Discovery**:
1. Scan `plugin_dirs` for `.py` files
2. Import modules and find `HookPlugin` subclasses
3. Instantiate and call `on_load()` with plugin config
4. Register handlers by event type and priority

**Handler Priorities**:
- Lower number = earlier execution
- Priority 0-49: Pre-handlers (can block)
- Priority 50: Core handler (existing HookManager)
- Priority 51-100: Post-handlers (observe only)

**Security Model**:
- Plugins disabled by default (`plugins.enabled: false`)
- Each plugin must be explicitly enabled in config
- Plugin errors logged but don't crash daemon
- Sandboxing: Future enhancement (restricted imports)

---

## Storage Schema

### Webhook Delivery Log (Optional)

```sql
CREATE TABLE webhook_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_url TEXT NOT NULL,
    event_type TEXT NOT NULL,
    session_id TEXT,
    payload TEXT NOT NULL,          -- JSON
    status_code INTEGER,
    response_body TEXT,
    delivered_at TIMESTAMP,
    retry_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_webhook_deliveries_endpoint ON webhook_deliveries(endpoint_url);
CREATE INDEX idx_webhook_deliveries_event ON webhook_deliveries(event_type);
```

---

## Implementation Checklist

### Phase 1: WebSocket Event Broadcasting (Core Done, Tests Pending)

#### Infrastructure Setup
- [x] Add `websocket_server` reference to HTTPServer class (`src/servers/http.py`)
- [x] Modify `GobbyRunner` to pass WebSocket server to HTTP server after init
- [x] Create `src/hooks/broadcaster.py` module

#### Broadcaster Implementation
- [x] Create `HookEventBroadcaster` class
- [x] Implement `broadcast_hook_event(event: HookEvent, response: HookResponse)` method
- [x] Implement event filtering based on config `broadcast_events` list
- [x] Implement payload sanitization (remove sensitive data option)
- [x] Add `include_payload` config option

#### Client Subscription (Decision 3)
- [ ] Define subscription message format: `{"type": "subscribe", "events": ["session_start", ...]}`
- [ ] Add `subscriptions` dict to WebSocket connection state
- [ ] Filter broadcasts based on client subscriptions (default: all events)
- [ ] Add `{"type": "unsubscribe", "events": [...]}` support
- [ ] Document subscription protocol in WebSocket event schema docs

#### Configuration
- [x] Add `HookExtensionsConfig` to `src/config/app.py`
- [x] Add `WebSocketBroadcastConfig` sub-config
- [x] Add default config values

#### Integration
- [x] Call broadcaster in `/hooks/execute` endpoint after handler returns
- [x] Add broadcast to `HookManager.handle()` for direct calls
- [x] Handle broadcast errors gracefully (log, don't fail)

#### Testing
- [ ] Unit tests for `HookEventBroadcaster`
- [ ] Integration test: WebSocket client receives hook events
- [ ] Test event filtering
- [ ] Test error handling when no clients connected

---

### Phase 2: Config-Driven Webhooks

#### Core Implementation
- [ ] Create `src/hooks/webhooks.py` module
- [ ] Create `WebhookDispatcher` class
- [ ] Implement `trigger(event_type, event, response)` async method
- [ ] Implement endpoint matching by event type
- [ ] Implement HTTP POST with configurable headers

#### Retry Logic
- [ ] Implement exponential backoff retry
- [ ] Add `timeout` per endpoint
- [ ] Add `retry_count` per endpoint
- [ ] Log failures with context

#### Blocking Webhooks
- [ ] Implement `can_block` option
- [ ] Parse webhook response for `decision` field
- [ ] Return `HookResponse` from blocking webhook
- [ ] Add timeout protection for blocking webhooks

#### Configuration
- [ ] Add `WebhooksConfig` to `src/config/app.py`
- [ ] Add `WebhookEndpointConfig` for endpoint definitions
- [ ] Support environment variable substitution in headers (`${VAR}`)
- [ ] Validate webhook URLs on config load

#### Integration
- [ ] Initialize `WebhookDispatcher` in HTTP server lifespan
- [ ] Call dispatcher in `/hooks/execute` endpoint
- [ ] Add webhook delivery logging (optional, if table created)

#### Fire-and-Forget Delivery (Decision 4)
- [ ] Implement async webhook dispatch (no blocking on response)
- [ ] Add `log_deliveries: true/false` config option (default: false)
- [ ] Document that webhook reliability is the endpoint's responsibility

#### Testing
- [ ] Unit tests for `WebhookDispatcher`
- [ ] Integration test with mock webhook server
- [ ] Test retry logic
- [ ] Test blocking webhook flow
- [ ] Test environment variable substitution

---

### Phase 3: Python Plugin System

#### Plugin Infrastructure
- [ ] Create `src/hooks/plugins.py` module
- [ ] Define `HookPlugin` base class with `name`, `version`, `on_load()`, `on_unload()`
- [ ] Define `@hook_handler` decorator with `event_type` and `priority` params
- [ ] Create `PluginLoader` class for discovery and loading
- [ ] Create `PluginRegistry` for handler registration

#### Plugin Discovery
- [ ] Implement `discover_plugins(dirs: list[str])` method
- [ ] Scan directories for `.py` files
- [ ] Import modules dynamically
- [ ] Find `HookPlugin` subclasses
- [ ] Handle import errors gracefully

#### Plugin Lifecycle
- [ ] Implement `load_plugin(path, config)` method
- [ ] Call `on_load()` with plugin-specific config
- [ ] Register handlers by event type
- [ ] Implement `unload_plugin(name)` method
- [ ] Call `on_unload()` on daemon shutdown

#### Handler Execution
- [ ] Implement `execute_handlers(event_type, event)` method
- [ ] Sort handlers by priority
- [ ] Execute pre-handlers (priority < 50) before core handler
- [ ] Execute post-handlers (priority >= 50) after core handler
- [ ] Short-circuit on `deny` response from pre-handler
- [ ] Pass handler errors to logger, don't fail

#### Configuration
- [ ] Add `PluginsConfig` to `src/config/app.py`
- [ ] Add `plugin_dirs` list
- [ ] Add `plugins` dict for per-plugin config and enablement
- [ ] Add `auto_discover` boolean

#### Integration
- [ ] Initialize `PluginLoader` in HTTP server lifespan
- [ ] Inject `PluginRegistry` into `HookManager`
- [ ] Modify `HookManager.handle()` to call plugin handlers
- [ ] Add plugin status to `/admin/status` endpoint

#### Testing
- [ ] Unit tests for `PluginLoader`
- [ ] Unit tests for `PluginRegistry`
- [ ] Integration test with sample plugin
- [ ] Test priority ordering
- [ ] Test deny short-circuit
- [ ] Test plugin error isolation

---

### Phase 4: Workflow Integration

#### Webhook as Workflow Action
- [ ] Add `webhook` action type to workflow engine
- [ ] Implement `WebhookAction` class in `src/workflows/actions/`
- [ ] Support `url`, `event`, `can_block`, `headers` params
- [ ] Integrate with existing webhook infrastructure

#### Plugin-Defined Actions
- [ ] Allow plugins to register custom workflow actions
- [ ] Add `register_action(name, handler)` to plugin interface
- [ ] Expose registered actions to workflow engine

#### Plugin-Defined Conditions
- [ ] Allow plugins to register custom condition evaluators
- [ ] Add `register_condition(name, evaluator)` to plugin interface
- [ ] Expose registered conditions for workflow `when` clauses

#### Documentation
- [ ] Document webhook action YAML syntax
- [ ] Document plugin action registration
- [ ] Add examples to workflow templates

---

### Phase 5: CLI & Monitoring

#### CLI Commands
- [ ] Add `gobby hooks` command group
- [ ] Implement `gobby hooks list` - show registered handlers
- [ ] Implement `gobby hooks test <event>` - trigger test event
- [ ] Implement `gobby plugins list` - show loaded plugins
- [ ] Implement `gobby plugins reload [plugin_name]` - reload all or specific plugin
- [ ] Implement `gobby webhooks list` - show configured webhooks
- [ ] Implement `gobby webhooks test <endpoint>` - test webhook delivery

#### Stateless Plugin Reload (Decision 5)
- [ ] Call `on_unload()` on existing plugin instances before reload
- [ ] Re-import modules and re-instantiate plugins
- [ ] Clear and re-register all handlers from reloaded plugins
- [ ] Document that reload clears plugin state (pytest model)

#### MCP Tools
- [ ] Add `list_hook_handlers()` MCP tool
- [ ] Add `test_hook_event(event_type, data)` MCP tool
- [ ] Add `list_plugins()` MCP tool
- [ ] Add `reload_plugins()` MCP tool

#### Observability
- [ ] Add hook event metrics (count by type)
- [ ] Add webhook delivery metrics (success/failure rate)
- [ ] Add plugin handler metrics (execution time)
- [ ] Expose metrics via `/admin/metrics` endpoint

---

### Phase 6: Documentation

- [ ] Update CLAUDE.md with hook extension configuration
- [ ] Create `docs/hook-extensions.md` user guide
- [ ] Document WebSocket event schema (including subscription protocol)
- [ ] Document webhook payload format
- [ ] Document plugin interface

#### Plugin Security Model (Decision 2)
- [ ] Add "Security Model" section explaining trust model
- [ ] Document: "Plugins run with full daemon privileges. Only enable plugins you trust."
- [ ] Add warning message when enabling plugins via config
- [ ] Consider `--i-trust-this-plugin` flag for explicit acknowledgment

#### Examples
- [ ] Add example plugins to `examples/plugins/`
- [ ] Add example webhook integration (Slack notification)

---

## Decisions

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| 1 | **Webhook authentication** | Header-based auth only for v1 | Covers 90% of use cases (Slack, Discord, PagerDuty). Add OAuth/signed payloads later if needed. |
| 2 | **Plugin sandboxing** | Defer - use trust model via documentation | Plugins disabled by default, require explicit enablement. Document: "Plugins run with full daemon privileges. Only enable plugins you trust." |
| 3 | **WebSocket client filtering** | Yes - add client-side subscription | Low-cost, high-value. Clients send `{"subscribe": ["session_start", "tool_call"]}` after connection. |
| 4 | **Webhook delivery guarantees** | Fire-and-forget with optional logging | Delivery logging table already planned. For guaranteed delivery, users can put a queue behind their endpoint. |
| 5 | **Plugin hot-reload** | Yes, stateless reload via CLI | Allow `gobby plugins reload` but document that plugin state is lost. This is the pytest model. |

---

## Future Enhancements

- **Plugin marketplace** - Share plugins via registry
- **Webhook templates** - Pre-built integrations (Slack, Discord, PagerDuty)
- **Event replay** - Replay historical events through webhooks
- **Plugin dependencies** - Plugins that depend on other plugins
- **Conditional broadcasting** - WebSocket clients specify event filters
- **Webhook batching** - Aggregate events into periodic batch calls
- **Plugin metrics dashboard** - Visualize plugin performance

# A2UI (Agent-to-UI) Implementation Plan

## Context

OpenClaw's Canvas/A2UI lets agents generate interactive HTML — buttons, forms, cards — that render in a visual workspace. User clicks on those elements become tool calls back to the agent, closing the loop without typing. Gobby's web UI is rich but developer-designed at build time. A2UI lets **agents design UI at runtime** for the current task.

Gobby already has the foundational pattern: `AskUserQuestion` blocks the agent, renders interactive options in the web UI, collects user input, and returns it as a tool result. A2UI generalizes this to arbitrary HTML content.

## Design Decisions

**Where does it render?** Inline in chat messages (same spot as AskUserQuestion), with a "pop out" button for complex canvases. Keeps the mental model simple — the agent "says" something interactive.

**How does it work across CLIs?** As an MCP tool (`gobby-canvas` registry). Any CLI agent calls `render_canvas` → content broadcasts to web UI via WebSocket → user interacts → result returns to agent. Works whether the agent is Claude Code in a terminal or the web chat.

**Multi-tab behavior:** `render_canvas` broadcasts the canvas to all open tabs subscribed to the conversation. A per-canvas `asyncio.Lock` ensures the first interaction wins — subsequent interactions receive an "already completed" error. All tabs must listen for `canvas_event` with `event: "completed"` to reconcile their UI. Secondary interaction attempts after completion are rejected at the lock and result in a UI refresh via the `canvas_event`, keeping all tabs consistent.

**Blocking model?** Same `asyncio.Event` pattern as `AskUserQuestion` (`chat_session.py:387-424`) and pipeline approvals. Tool blocks until user interacts or timeout expires.

**WebSocket disconnection handling:** When a WebSocket disconnects, the `on_disconnect` handler calls `canvas_registry.cancel_conversation_canvases(conversation_id)` to cancel any pending canvases for that conversation. For each pending canvas: mark `completed=True`, set `interaction_result={"error": "websocket_disconnected"}`, and trigger `pending_event.set()` to wake the awaiting coroutine. This ensures blocking `render_canvas` calls receive an immediate error instead of waiting for timeout.

**Security?** Server-side: `nh3>=0.3.3` (Rust-backed ammonia) for allowlist sanitization — strip `<script>`, `on*` handlers, `javascript:` URIs before storage. Client-side: DOMPurify as defense-in-depth with strict allowlist. No Bleach (unmaintained).

## Data Flow

```
Agent (any CLI) ──call_tool("gobby-canvas", "render_canvas", {content, blocking})──▶ MCP Proxy
    │
    ▼
Canvas Registry (canvas.py)
    │ stores CanvasState, creates asyncio.Event, broadcasts via callback
    ▼
WebSocket Server ──broadcast canvas_event──▶ Web UI (useChat.ts)
    │                                            │
    │                                            ▼
    │                                       ToolCallDisplay.tsx detects render_canvas
    │                                       CanvasRenderer.tsx sanitizes + renders HTML
    │                                            │
    │                                       User clicks [data-action="approve"]
    │                                            │
    │  ◀──canvas_interaction ws message──────────┘
    ▼
WebSocket handler (_handle_canvas_interaction)
    │ looks up CanvasState, sets asyncio.Event
    ▼
Canvas Registry unblocks, returns {action, payload} to agent
```

## Implementation

### Phase 1: Core Loop (MVP)

**7 new files, 5 modified files**

#### Backend

**New: `src/gobby/mcp_proxy/tools/canvas.py`** — Canvas tool registry

```python
# Pattern: follows create_memory_registry() in tools/memory.py
def create_canvas_registry(
    broadcast_callback: Callable | None = None,
) -> InternalToolRegistry:
```

Three tools:
- `render_canvas(content: str, canvas_id: str = "", title: str = "", blocking: bool = True, timeout: int = 600)` — Clamp timeout first: `timeout = max(1, min(3600, timeout))`. Sanitize HTML server-side using `nh3` (**mandatory** — strip `<script>`, `on*` handlers, `javascript:` URIs via allowlist before storage). Validate raw byte size: `len(content.encode('utf-8'))` against `MAX_CANVAS_SIZE` **before** sanitization to reject oversized payloads early. Store `CanvasState`, broadcast `canvas_event` with `event: "rendered"`. If blocking, wait on `asyncio.Event` using the clamped timeout. Return `{canvas_id, interaction: {action, payload, form_data}}` or `{canvas_id, status: "rendered"}` for non-blocking
- `update_canvas(canvas_id: str, content: str, mode: str = "replace")` — Update stored content, broadcast `canvas_event` with `event: "updated"`, return immediately
- `clear_canvas(canvas_id: str)` — Remove canvas state, broadcast `event: "cleared"`

In-memory state:
```python
@dataclass
class CanvasState:
    canvas_id: str
    content: str
    title: str
    conversation_id: str  # for routing interactions
    pending_event: asyncio.Event | None  # for blocking mode
    interaction_result: dict | None
    created_at: datetime
    expires_at: datetime  # created_at + timeout; for automatic cleanup
    completed: bool = False  # True after interaction received or timeout
```

Key: `_pending_canvases: dict[str, CanvasState]` shared between the tool functions and the resolve function that the WebSocket handler calls.

Rate limiting and resource bounds:
```python
MAX_CANVASES_PER_CONVERSATION = 50  # prevent runaway agents
MAX_TOTAL_CANVASES = 1000           # server-wide cap
MAX_CANVAS_SIZE = 64 * 1024         # 64KB HTML content limit
MAX_RENDER_RATE = 10                # renders per minute per conversation
MAX_PAYLOAD_DEPTH = 10              # max nesting depth for payload/form_data
```

**Sliding-window rate limiter**: Maintain a per-conversation timestamp list (`_render_timestamps: dict[str, list[datetime]]`). On each `render_canvas` call, prune timestamps older than 1 minute, reject if `len >= MAX_RENDER_RATE`, otherwise append `now()`. Thread-safe via a dedicated per-conversation `asyncio.Lock` (`_rate_limit_locks: dict[str, asyncio.Lock]`), **not** the per-canvas lock — multiple concurrent `render_canvas` calls for different canvases in the same conversation would use different per-canvas locks, leaving the shared timestamp list unprotected.

Enforce in `render_canvas` **before sanitization**: check `len(content.encode('utf-8'))` against `MAX_CANVAS_SIZE` to reject oversized payloads early. Then reject if conversation exceeds `MAX_CANVASES_PER_CONVERSATION`, global count exceeds `MAX_TOTAL_CANVASES`, or rate limit hit.

**Sweeper lifecycle**: Start the background canvas sweeper when the WebSocket server lifespan begins. Store the `asyncio.Task` reference in server state so only one task exists. Cancel and await that task on server shutdown. The sweeper loop (`_canvas_sweeper(registry, shutdown_event)`) checks `shutdown_event.is_set()`, wraps `registry.sweep_expired()` in try/except (logging errors), and awaits `asyncio.sleep(60)` between runs. `render_canvas` still performs a lazy sweep as fallback.

**Registry API**:
- `get_canvas(canvas_id: str) -> CanvasState | None` — Retrieve canvas state by ID, or None if not found. Returns `_pending_canvases.get(canvas_id)`.
- `resolve_interaction(canvas_id, action, payload, form_data)` — Unblock a pending canvas. Uses a per-canvas `asyncio.Lock` (`_canvas_locks: dict[str, asyncio.Lock]`) to ensure atomicity: fetch canvas from `_pending_canvases`, check `canvas.completed` (raise if already completed), set `completed=True` and store `interaction_result`, then trigger `pending_event.set()`.
- `cancel_conversation_canvases(conversation_id: str)` — Cancel all pending canvases for a conversation (used on WebSocket disconnect).

**Modify: `src/gobby/mcp_proxy/registries.py`** (line ~374, after pipelines registry)

```python
from gobby.mcp_proxy.tools.canvas import create_canvas_registry

canvas_registry = create_canvas_registry()
manager.add_registry(canvas_registry)
```

Store a reference to the registry so the WebSocket server can call `resolve_interaction`. Pattern: same as how `pipeline_executor` is passed around — add `canvas_registry` to `setup_internal_registries` return or expose it on the manager.

**Modify: `src/gobby/servers/websocket/server.py`** (line 209-229, dispatch table)

Add entry:
```python
"canvas_interaction": self._handle_canvas_interaction,
```

**Modify: `src/gobby/servers/websocket/chat.py`** — Add handler

```python
async def _handle_canvas_interaction(self, websocket: Any, data: dict[str, Any]) -> None:
    canvas_id = data.get("canvas_id")
    action = data.get("action", "")
    payload = data.get("payload", {})
    form_data = data.get("form_data", {})

    # Validate required fields
    if not canvas_id or not isinstance(canvas_id, str):
        await self._send_error(websocket, "canvas_interaction requires a valid canvas_id")
        return
    if not action or not isinstance(action, str):
        await self._send_error(websocket, "canvas_interaction requires a valid action")
        return
    if not isinstance(payload, dict) or not isinstance(form_data, dict):
        await self._send_error(websocket, "payload and form_data must be objects")
        return
    # Recursive depth validation for nested structures
    if not _validate_payload_depth(payload) or not _validate_payload_depth(form_data):
        await self._send_error(websocket, "payload or form_data too deeply nested")
        return

    canvas_registry = getattr(self, "_canvas_registry", None)
    if not canvas_registry:
        await self._send_error(websocket, "Canvas system not available")
        return

    # Ownership check: derive conversation_id from authenticated session context,
    # NOT from the untrusted client payload. This prevents spoofing.
    conversation_id = self._get_conversation_id(websocket)  # server-side session state
    canvas_state = canvas_registry.get_canvas(canvas_id)
    if canvas_state and canvas_state.conversation_id != conversation_id:
        await self._send_error(websocket, "Canvas does not belong to this conversation")
        return

    try:
        canvas_registry.resolve_interaction(canvas_id, action, payload, form_data)
    except KeyError:
        await self._send_error(websocket, f"Canvas {canvas_id} not found or already completed")
    except Exception as e:
        logger.error(f"Canvas interaction failed for {canvas_id}: {e}", exc_info=True)
        await self._send_error(websocket, "Internal error processing canvas interaction")
```

Depth validation helper:
```python
def _validate_payload_depth(obj: Any, max_depth: int = 10, current_depth: int = 0) -> bool:
    """Reject too-deep nesting in payload/form_data to prevent resource exhaustion."""
    if current_depth >= max_depth:
        return False
    if isinstance(obj, dict):
        return all(_validate_payload_depth(v, max_depth, current_depth + 1) for v in obj.values())
    if isinstance(obj, list):
        return all(_validate_payload_depth(v, max_depth, current_depth + 1) for v in obj)
    return True
```

Wire `_canvas_registry` on WebSocketServer during HTTP server lifespan (same pattern as `workflow_handler`).

**Modify: `src/gobby/servers/websocket/broadcast.py`** — Add `canvas_event` to high-volume subscription list

#### Frontend

**New: `web/src/utils/sanitizeHtml.ts`** — DOMPurify config

```typescript
import DOMPurify from 'dompurify'

const ALLOWED_TAGS = [
  'div', 'span', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'button', 'input', 'select', 'option', 'textarea', 'form', 'label',
  'table', 'tr', 'td', 'th', 'thead', 'tbody', 'ul', 'ol', 'li',
  'a', 'img', 'br', 'hr', 'strong', 'em', 'code', 'pre',
  'details', 'summary',
]

const ALLOWED_ATTRS = [
  'class', 'id', 'style', 'type', 'name', 'value', 'placeholder',
  'disabled', 'checked', 'selected', 'href', 'src', 'alt', 'title',
  'data-action', 'data-payload', 'data-element-id',
  'aria-label', 'aria-describedby', 'role',
  'rows', 'cols', 'min', 'max', 'step', 'pattern', 'required',
]

// Comprehensive list to prevent UI redressing/clickjacking and performance exploits
const FORBIDDEN_CSS_PROPERTIES = [
  'position', 'z-index', 'opacity', 'pointer-events', 'cursor',
  'transform', 'filter', 'backdrop-filter', 'mix-blend-mode',
  'clip-path', 'mask', 'animation', 'transition',
  'overflow', 'isolation', 'contain', 'will-change',
  'backface-visibility', 'perspective', 'perspective-origin',
]
const ALLOWED_URI_REGEXP = /^(?:https?|data):/i

export function sanitizeCanvasHtml(html: string): string {
  const clean = DOMPurify.sanitize(html, {
    ALLOWED_TAGS, ALLOWED_ATTR: ALLOWED_ATTRS,
    ALLOWED_URI_REGEXP,
  })
  // Post-process: parse result into DOM, strip forbidden CSS, validate URIs
  const doc = new DOMParser().parseFromString(clean, 'text/html')
  doc.querySelectorAll('[style]').forEach(el => {
    const style = (el as HTMLElement).style
    FORBIDDEN_CSS_PROPERTIES.forEach(prop => style.removeProperty(prop))
    if (!style.length) el.removeAttribute('style')
  })
  // Validate href/src against allowed URI schemes
  doc.querySelectorAll('[href],[src]').forEach(el => {
    for (const attr of ['href', 'src']) {
      const val = el.getAttribute(attr)
      if (val && !ALLOWED_URI_REGEXP.test(val)) el.removeAttribute(attr)
    }
  })
  // Validate data-payload depth after parse
  doc.querySelectorAll('[data-payload]').forEach(el => {
    try {
      const parsed = JSON.parse(el.getAttribute('data-payload')!)
      if (!validatePayloadDepth(parsed)) el.removeAttribute('data-payload')
    } catch { el.removeAttribute('data-payload') }
  })
  return doc.body.innerHTML
}

function validatePayloadDepth(obj: unknown, maxDepth = 10, depth = 0): boolean {
  if (depth >= maxDepth) return false
  if (Array.isArray(obj)) return obj.every(v => validatePayloadDepth(v, maxDepth, depth + 1))
  if (obj && typeof obj === 'object')
    return Object.values(obj).every(v => validatePayloadDepth(v, maxDepth, depth + 1))
  return true
}
```

**New: `web/src/components/CanvasRenderer.tsx`** — Renders sanitized HTML with event delegation

```typescript
interface CanvasRendererProps {
  canvasId: string
  content: string
  title?: string
  status: 'active' | 'completed'
  onInteraction: (canvasId: string, action: string, payload: Record<string, unknown>, formData: Record<string, string>) => void
}
```

Key behaviors:
- `dangerouslySetInnerHTML` with DOMPurify-sanitized content
- Delegated click handler on container: walk up DOM from `e.target` to find closest `[data-action]`, with max traversal depth (20 levels) to prevent pathological DOM structures from hanging the handler
- Null-check `e.target` and verify it's within the container before traversal
- **Client-side throttling**: Debounce interaction callbacks (300ms) to prevent rapid-fire clicks from flooding the WebSocket
- **Payload-size validation**: Reject `data-payload` values exceeding `MAX_CANVAS_SIZE` (64KB) before parsing — `data-payload` is a subset of the canvas content and shares the same limit
- For `<form data-action="...">`: intercept submit, gather all named inputs
- For `<select data-action="...">`: intercept change events
- Parse `data-payload` with try/catch around `JSON.parse` — reject malformed JSON and log a warning rather than crashing the handler
- When `status === 'completed'`: dim the canvas, show interaction result
- Scoped CSS: `.canvas-container`, `.canvas-content`, themed buttons/inputs using existing CSS variables

**New: `web/src/components/CanvasRenderer.css`** — Styles following existing patterns (CSS variables, BEM-like naming)

**Modify: `web/src/components/ToolCallDisplay.tsx`** — Detect canvas tools and render inline

Add detection (follows `isAskUserQuestion` pattern at line 21):
```typescript
function isCanvasRender(call: ToolCall): boolean {
  // Direct tool name or routed through call_tool
  return call.tool_name === 'render_canvas' ||
    (call.tool_name === 'mcp__gobby__call_tool' &&
     call.arguments?.tool_name === 'render_canvas')
}
```

Add `CanvasDisplay` component (parallel to `AskUserQuestionDisplay`), rendered in `ToolCallItem` at line 158.

Thread `onCanvasInteraction` prop through `ToolCallDisplay` → `ToolCallItem` → `CanvasDisplay` → `CanvasRenderer` (same as `onRespond` for AskUserQuestion).

**Modify: `web/src/hooks/useChat.ts`** — Add canvas interaction callback

```typescript
const respondToCanvas = useCallback((
  canvasId: string, action: string,
  payload: Record<string, unknown>, formData: Record<string, string>
) => {
  const ws = wsRef.current
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn('Canvas interaction dropped: WebSocket not connected')
    return
  }
  // Validate conversationId is set before sending
  const convId = conversationIdRef.current
  if (!convId) {
    console.warn('Canvas interaction dropped: no active conversation')
    return
  }
  // Three-state flow: active → pending → completed (or back to active on failure)
  updateCanvasStatus(canvasId, 'pending')  // show loading indicator
  // Revert to active after 5s if no server confirmation received
  const pendingTimeout = setTimeout(() => {
    updateCanvasStatus(canvasId, 'active')
    console.warn(`Canvas ${canvasId} interaction timed out, reverting`)
  }, 5000)
  try {
    ws.send(JSON.stringify({
      type: 'canvas_interaction',
      canvas_id: canvasId, action, payload, form_data: formData,
    }))
    // Note: conversation_id is NOT sent — server derives it from
    // the authenticated WebSocket session to prevent spoofing.
    // Transition to 'completed' happens on server confirmation
    // via canvas_event with event: "interaction_confirmed"
  } catch (e) {
    clearTimeout(pendingTimeout)
    updateCanvasStatus(canvasId, 'active')
    console.error('Failed to send canvas interaction:', e)
  }
}, [])
```

Handle `canvas_event` messages in the WebSocket message handler (e.g., the switch/if in `useChat.ts` that processes incoming messages):
```typescript
if (msg.type === 'canvas_event') {
  if (msg.event === 'interaction_confirmed') {
    clearCanvasRevertTimeout(msg.canvas_id)  // cancel pending timeout
    updateCanvasStatus(msg.canvas_id, 'completed')
  } else if (msg.event === 'updated') {
    updateCanvasContent(msg.canvas_id, msg.content)
  } else if (msg.event === 'cleared') {
    removeCanvas(msg.canvas_id)
  }
}
```

**New dependency: `dompurify` + `@types/dompurify`** in `web/package.json`

### Phase 2: Live Updates + Side Panel

- `update_canvas` broadcasts partial updates; frontend patches DOM by `canvas_id`
- Non-blocking canvases: interactions injected as synthetic user messages instead of unblocking events. **Synthetic messages must be tagged with `{ source: 'canvas', canvas_id, metadata: { action, timestamp } }` to distinguish them from real user input.** Payloads must be sanitized (strip HTML, limit length) and rate-limited (max 1 synthetic message per canvas per second)
- `CanvasPanel.tsx` — collapsible side panel (follows `TerminalPanel` pattern in `ChatPage.tsx`) for expanded/multi-canvas view

### Phase 3: Persistence + Templates

- PostgreSQL `canvas_states` table for surviving session restarts
- REST endpoints (`/canvas/{conversation_id}`) following `create_*_router()` pattern
- Canvas templates: agent calls `render_canvas(template="approval_form", variables={...})`

## Data Attribute Convention

```html
<button data-action="approve" data-payload='{"id":"123"}'>Approve</button>
<form data-action="submit_config">
  <input name="threshold" type="number" value="80" />
  <button type="submit">Save</button>
</form>
```

- `data-action` — declares this element triggers an interaction. **Values must match `VALID_ACTION_PATTERN = /^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/`** (alphanumeric + hyphens/underscores, 1-64 chars, starts with letter). The frontend rejects actions that don't match and logs a warning. This prevents injection of arbitrary strings as action names
- `data-payload` — JSON string sent with the action. **Must be valid JSON.** The frontend parses with `JSON.parse` inside a try/catch; malformed values are ignored with a console warning. Maximum `MAX_CANVAS_SIZE` (64KB). Parsed payloads are validated for nesting depth (`validatePayloadDepth`, max 10 levels) to prevent JSON bombs — payloads exceeding depth are stripped. Agents should use `JSON.stringify()` to produce this value, never string concatenation, to prevent injection via crafted payloads
- `data-element-id` — for targeted updates via `update_canvas`

## Files Summary

### New files (Phase 1)
| File | Purpose |
|------|---------|
| `src/gobby/mcp_proxy/tools/canvas.py` | Canvas tool registry |
| `web/src/components/CanvasRenderer.tsx` | HTML renderer with event delegation |
| `web/src/components/CanvasRenderer.css` | Canvas styles |
| `web/src/utils/sanitizeHtml.ts` | DOMPurify config |
| `tests/tools/test_canvas.py` | Backend unit tests |
| `tests/websocket/test_canvas_interaction.py` | WebSocket integration tests |

### Modified files (Phase 1)
| File | Change |
|------|--------|
| `src/gobby/mcp_proxy/registries.py` | Add canvas registry (~374) |
| `src/gobby/servers/websocket/server.py` | Add `canvas_interaction` to dispatch (line 209) |
| `src/gobby/servers/websocket/chat.py` | Add `_handle_canvas_interaction` handler |
| `src/gobby/servers/websocket/broadcast.py` | Add `canvas_event` to high-volume types |
| `web/src/components/ToolCallDisplay.tsx` | Add `isCanvasRender` + `CanvasDisplay` |
| `web/src/hooks/useChat.ts` | Add `respondToCanvas` + `canvas_event` handler |
| `web/package.json` | Add `dompurify` dependency |

## Audit Logging

All canvas lifecycle events should be logged with structured fields for observability and debugging:

```python
logger.info("canvas_rendered", extra={
    "canvas_id": canvas_id, "conversation_id": conversation_id,
    "content_size": len(content), "blocking": blocking,
})
logger.info("canvas_interaction", extra={
    "canvas_id": canvas_id, "action": action,
    "conversation_id": conversation_id,
})
logger.info("canvas_expired", extra={"canvas_id": canvas_id})
```

## Verification

1. **Unit tests**: `tests/tools/test_canvas.py` — test render_canvas creates state, resolve_interaction unblocks, timeout returns error, clear_canvas cleans up
2. **Integration test**: `tests/websocket/test_canvas_interaction.py` — test full WebSocket round-trip: render → broadcast → interact → unblock
3. **Manual E2E**: Start daemon, open web UI, start a chat. Agent calls `render_canvas` with a simple button. Verify:
   - Canvas renders inline in chat
   - Button is clickable
   - Click sends `canvas_interaction` via WebSocket
   - Agent receives interaction result and continues
4. **Security tests**: Verify `<script>alert(1)</script>` is stripped, `onclick` handlers are stripped, `javascript:` URIs are stripped, all FORBIDDEN_CSS_PROPERTIES are removed from inline styles, oversized content is rejected, invalid `data-action` values are rejected, deeply nested payloads (>10 levels) are stripped by `validatePayloadDepth`
5. **Cross-CLI**: From a terminal Claude Code session, call `render_canvas` via MCP. Verify web UI shows the canvas and interaction flows back
6. **Concurrency tests**: Verify concurrent `render_canvas` calls from the same conversation respect `MAX_CANVASES_PER_CONVERSATION`, global count respects `MAX_TOTAL_CANVASES`, concurrent interactions on the same canvas are serialized by per-canvas `asyncio.Lock` (first wins, second gets error), and the background sweeper correctly cleans up expired canvases under load
7. **Ownership tests**: Verify ownership is derived from server-side session context (not client payload), and a canvas interaction from a different conversation's WebSocket is rejected
8. **Disconnect tests**: Verify that when a WebSocket disconnects, all pending canvases for that conversation are cancelled, blocking `render_canvas` calls unblock with `{"error": "websocket_disconnected"}`, and the UI transitions canvases to error state
9. **Rate limiting tests**: Verify sliding-window rate limiter rejects renders exceeding `MAX_RENDER_RATE` per minute per conversation, and that timestamps are correctly pruned after the window expires

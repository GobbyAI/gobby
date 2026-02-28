# A2UI Canvas Platform — Phase 1

## Context

The current canvas approach (`docs/plans/a2ui-canvas.md`, now abandoned) used raw HTML with `data-action`/`data-payload` conventions. Two problems: agents don't know how to use the convention (no skill), and HTML sanitization is complex and fragile (dual sanitization, CSS blocklists, URI filters).

This plan replaces it with a full canvas platform built on Google's A2UI v0.8 protocol. Two rendering modes:

- **Inline A2UI surfaces** — declarative JSON components rendered as React (forms, approvals, status cards). No HTML sanitization needed — agents produce JSON, client renders trusted React components.
- **Canvas panel** — sandboxed iframe panel for arbitrary HTML content. Security via `sandbox="allow-scripts"` (no same-origin access).

## Constraints

- Phase 1 only (core loop). Phases 2-3 (eval/snapshot/navigate, persistence/templates) are future work.
- No `dangerouslySetInnerHTML` for inline A2UI.
- No new npm dependency for sanitization (no dompurify needed).
- Backend validates component types against catalog — unknown types rejected.
- 18 new files, 10 modified files.

## Phase 1: Skill Infrastructure

**Goal**: Enable skills to filter by session source so the canvas skill only injects for web UI sessions.

### 1.1 Add `sources` field to skill audience filtering [category: code]

Target: `src/gobby/skills/parser.py`, `src/gobby/skills/injector.py`

Add `sources: list[str] | None = None` to `SkillAudienceConfig` (line 49 in parser.py). Add source matching to `_matches_audience()` (line 192 in injector.py). Update parser to extract `sources` from `metadata.gobby` namespace.

**parser.py** — `SkillAudienceConfig` (line 49):
```python
@dataclass
class SkillAudienceConfig:
    audience: str = "all"
    depth: int | list[int] | str | None = None
    steps: list[str] | None = None
    task_categories: list[str] | None = None
    sources: list[str] | None = None  # NEW: Session source filter, e.g. ["claude_sdk_web_chat"]
    format_overrides: dict[str, str] | None = None
    priority: int = 50
```

**parser.py** — add to `parse_skill_text` gobby_meta parsing block (~line 327-370):
```python
if "sources" in gobby_meta:
    raw_sources = gobby_meta["sources"]
    if isinstance(raw_sources, list):
        ac_kwargs["sources"] = [str(s) for s in raw_sources]
    elif isinstance(raw_sources, str):
        ac_kwargs["sources"] = [raw_sources]
```

Also add `"sources"` to `ParsedSkill.to_dict()` serialization (~line 178).

**injector.py** — `_matches_audience()`, after task_categories check (line 212):
```python
# Source check (web UI vs CLI)
if config.sources is not None:
    if context.source not in config.sources:
        return False
```

Existing infrastructure:
- `AgentContext.source` already populated from session (injector.py:34, extracted at lines 50-53)
- `SessionSource.CLAUDE_SDK_WEB_CHAT = "claude_sdk_web_chat"` (events.py:65)
- Web chat sets `source="claude_sdk_web_chat"` at creation (chat.py:191)

**Behavioral specs**:
- `sources: None` (default) = match any source (backward compatible)
- `sources: ["claude_sdk_web_chat"]` = only web UI sessions
- Context with `source=None` does NOT match an explicit sources list

**Tests** (new tests in `tests/skills/test_parser.py` and `tests/skills/test_injector.py`):
- `test_sources_none_matches_any` — default None matches all
- `test_sources_list_matches` — explicit list matches matching source
- `test_sources_list_rejects` — explicit list rejects non-matching source
- `test_sources_none_context_rejects` — context source=None rejects explicit list
- `test_parse_sources_from_frontmatter` — parser extracts sources
- `test_parse_sources_single_string` — coerces string to list

## Phase 2: Backend Canvas System

**Goal**: Create the `gobby-canvas` tool registry with state management, 6 tools, rate limiting, and wire it into the daemon.

### 2.1 Create canvas tool registry [category: code]

Target: `src/gobby/mcp_proxy/tools/canvas.py` (new)

Pattern: Follow `create_memory_registry()` in `src/gobby/mcp_proxy/tools/memory.py` (lines 43-601). Factory returns `InternalToolRegistry`, uses `@registry.tool()` decorator.

**CanvasState**:
```python
@dataclass
class CanvasState:
    canvas_id: str
    mode: str                     # "a2ui" or "html"
    surface: dict[str, Any]       # component map {id: component_def}
    data_model: dict[str, Any]    # bound data
    root_component_id: str | None
    html_url: str | None          # URL for html mode
    conversation_id: str
    pending_event: asyncio.Event | None
    interaction_result: dict | None
    created_at: datetime
    expires_at: datetime
    completed: bool = False
```

**Constants**:
```python
MAX_CANVASES_PER_CONVERSATION = 50
MAX_TOTAL_CANVASES = 1000
MAX_COMPONENT_COUNT = 200
MAX_DATA_MODEL_SIZE = 64 * 1024  # 64KB
MAX_RENDER_RATE = 10             # per minute per conversation
CANVAS_DEFAULT_TIMEOUT = 300.0   # 5 minutes
CANVAS_MAX_TIMEOUT = 600.0       # 10 minutes
A2UI_CATALOG = {"Text", "Button", "TextField", "CheckBox", "Row", "Column",
                "Card", "List", "Image", "Icon", "Badge"}
```

**Module state** (managed by closure):
```python
_canvases: dict[str, CanvasState] = {}
_canvas_locks: dict[str, asyncio.Lock] = {}   # per-canvas for first-wins
_rate_counters: dict[str, list[float]] = {}    # conversation_id -> timestamps
```

**Factory**:
```python
def create_canvas_registry(
    broadcaster: Callable[..., Awaitable[None]] | None = None,
) -> InternalToolRegistry:
```

**5 Tools**:

1. `render_surface(components, root_id, canvas_id?, data_model?, blocking=True, timeout=300, conversation_id?)` — Validate types against A2UI_CATALOG, validate component count/data model size, check rate limit, store CanvasState(mode="a2ui"), broadcast `surface_update`, optionally block on asyncio.Event. Note: `conversation_id` is automatically inferred from `context.session_id` if omitted.

2. `update_surface(canvas_id, components?, data_model?)` — Canvas must exist + not completed. Merge components dict, merge data_model (shallow). Validate after merge. Broadcast `surface_update`. Note: `conversation_id` inferred if omitted for rate-limiting.

3. `close_canvas(canvas_id)` — Checks mode ("a2ui" or "html"). For A2UI, mark completed, clear pending event, broadcast `close_canvas`, remove from internal canvas state. For html, mark completed, broadcast `close_canvas`.

4. `wait_for_interaction(canvas_id, timeout=300)` — Canvas must exist + not completed. Create asyncio.Event if absent, await with timeout.

5. `canvas_present(file_path, canvas_id?, title?, width?, height?, conversation_id?)` — Takes an absolute `file_path`. Backend reads file and writes to `canvas_dir/{uuid}.html` (never serves workspace directory directly). Creates CanvasState(mode="html", html_url="/__gobby__/canvas/{uuid}.html") and broadcasts `panel_present`. Note: `conversation_id` inferred if omitted.

**Registry API** (module-level functions, not tools):
- `get_canvas(canvas_id) -> CanvasState | None`
- `get_active_canvases(conversation_id) -> list[CanvasState]` — for WebSocket connect rehydration
- `resolve_interaction(canvas_id, action) -> bool` — per-canvas lock, first-wins semantics
- `cancel_conversation_canvases(conversation_id) -> int` — for WebSocket disconnect
- `sweep_expired() -> int` — remove expired canvases
- `set_broadcaster(callback)` — set broadcaster after creation (wired in HTTP lifespan)

**Rate limiting**: Sliding window per conversation — prune timestamps > 60s old, reject if count >= MAX_RENDER_RATE.

**Tests** (`tests/mcp_proxy/tools/test_canvas.py`):
- render creates state, rejects unknown types, enforces rate limit, blocking unblocks on resolve, timeout returns error
- update merges components/data_model, rejects on completed
- close_canvas removes state and broadcasts close
- wait_for_interaction creates event and blocks
- resolve_interaction first-wins (second returns False)
- cancel_conversation_canvases cancels all for convo
- sweep_expired removes expired
- canvas_present safely copies file to canvas dir and creates html state
- max component count enforced, max data model size enforced

### 2.2 Wire canvas registry into daemon infrastructure [category: code] (depends: 2.1)

Target: `src/gobby/mcp_proxy/registries.py`, `src/gobby/servers/websocket/server.py`, `src/gobby/servers/websocket/chat.py`, `src/gobby/servers/websocket/broadcast.py`, `src/gobby/servers/http.py`

**registries.py** (after line 382, after plugins registry):
```python
from gobby.mcp_proxy.tools.canvas import create_canvas_registry
canvas_registry = create_canvas_registry()
manager.add_registry(canvas_registry)
```

**server.py** (line 233, dispatch table):
```python
"canvas_interaction": self._handle_canvas_interaction,
```

**chat.py** (add rehydration logic on connect, and new interaction method):
```python
# In websocket connection handler (e.g., near line 160):
from gobby.mcp_proxy.tools.canvas import get_active_canvases
active_canvases = get_active_canvases(self.session_id)
if active_canvases:
    await websocket.send_json({"route": "canvas_event", "event": "canvas_rehydrate", "surfaces": [c.__dict__ for c in active_canvases]})

# New method after `_handle_ask_user_response`:
async def _handle_canvas_interaction(self, websocket, data):
    canvas_id = data.get("canvas_id")
    action = data.get("action", {})
    if not canvas_id:
        return
    from gobby.mcp_proxy.tools.canvas import resolve_interaction
    resolved = await resolve_interaction(canvas_id, action)
    if resolved and self.broadcaster:
        await self.broadcaster.broadcast_canvas_event(
            event="interaction_confirmed", canvas_id=canvas_id, action=action)
```

**broadcast.py** — Add `"canvas_event"` to high-volume set (line 49). Add `broadcast_canvas_event(event, canvas_id, conversation_id?, **kwargs)` method.

**http.py** — Three changes:
1. Mount canvas files: `app.mount("/__gobby__/canvas", StaticFiles(directory=canvas_dir))` after line 631
2. Wire broadcaster in lifespan (~line 405): `set_broadcaster(ws.broadcast_canvas_event)`
3. Start sweeper task in lifespan, cancel on shutdown

## Phase 3: Frontend A2UI Components

**Goal**: Create the TypeScript types, component registry, all 11 A2UI components, renderer, and data model hook.

### 3.1 A2UI types, component registry, and all 11 components [category: code]

Target: `web/src/components/canvas/` (new directory)

**types.ts** — Core types:
- `BoundValue`: `{literalString?: string, path?: string}` — path is JSON pointer into data_model
- `Action`: `{name: string, context?: Record<string, BoundValue>}`
- `ChildrenSpec`: `{explicitList?: string[]}` — ordered child component IDs
- `A2UIComponentDef`: `{type: string, text?: BoundValue, label?: BoundValue, actions?: Action[], children?: ChildrenSpec, ...}`
- `A2UISurfaceState`: `{canvasId, conversationId, mode, surface: Record<string, A2UIComponentDef>, dataModel, rootComponentId, completed}`
- `UserAction`: `{name, sourceComponentId, timestamp, context}`
- `CanvasEvent`: `{type, event, canvas_id, ...payload}`
- `A2UIComponentProps`: `{componentId, def, surface, dataModel, onAction, completed}`

Also includes `resolveBoundValue(bv, dataModel)` and `resolveActionContext(context, dataModel)` utility functions.

**A2UIComponentRegistry.tsx** — Maps type names to React components:
```typescript
const COMPONENT_MAP: Record<string, A2UIComponent> = {
  Text: A2UIText, Button: A2UIButton, TextField: A2UITextField,
  CheckBox: A2UICheckBox, Row: A2UIRow, Column: A2UIColumn,
  Card: A2UICard, List: A2UIList, Image: A2UIImage,
  Icon: A2UIIcon, Badge: A2UIBadge,
}
```
Exports `RenderComponent` (renders single component by type, securely wrapped in an `ErrorBoundary` to prevent malformed components from crashing the chat UI) and `RenderChildren` (renders child list).

**11 Components** (each in `components/` subdirectory, all use `A2UIComponentProps`):

| Component | Renders as | Key behavior |
|-----------|-----------|--------------|
| `A2UIText` | `<h1>`-`<h3>` or `<p>` via usageHint | Text content only, no innerHTML |
| `A2UIButton` | CVA `Button` from `chat/ui/Button.tsx` | Fires first action on click, disabled when completed |
| `A2UITextField` | `<input>` with label | Updates data_model path on change |
| `A2UICheckBox` | `<input type="checkbox">` | Updates data_model path on change |
| `A2UIRow` | `div.flex.flex-row.gap-2` | RenderChildren |
| `A2UIColumn` | `div.flex.flex-col.gap-2` | RenderChildren |
| `A2UICard` | `div.rounded-lg.border.border-border.p-3` | RenderChildren |
| `A2UIList` | `<ul>` with children as `<li>` | RenderChildren |
| `A2UIImage` | `<img>` | src validated: https:// or data:image/ only |
| `A2UIIcon` | SVG from built-in icon set | check, x, alert, info, arrow-right, etc. |
| `A2UIBadge` | CVA `Badge` from `chat/ui/Badge.tsx` | Text content with variant |

**Edge cases**: Unknown types render error badge. BoundValue path to nonexistent key returns "". Missing child IDs silently skipped. Max render depth 20 for RenderChildren.

### 3.2 A2UIRenderer + useA2UIDataModel + barrel export [category: code] (depends: 3.1)

Target: `web/src/components/canvas/A2UIRenderer.tsx`, `web/src/components/canvas/hooks/useA2UIDataModel.ts`, `web/src/components/canvas/index.ts`

**useA2UIDataModel.ts** — State management for data model bound values:
- `dataModel` state initialized from surface
- `updateField(path, value)` — immutable update by JSON pointer path
- `mergeDataModel(updates)` — shallow merge from server updates
- `resetDataModel(newModel)` — full replace

**A2UIRenderer.tsx** — Top-level renderer:
```typescript
interface A2UIRendererProps {
  surface: A2UISurfaceState
  onAction: (canvasId: string, action: UserAction) => void
}
```
- Resolves root component from `surface.rootComponentId`
- Container: `rounded-lg border border-accent/30 bg-accent/5 p-3` (matches AskUserQuestionCard styling)
- When `completed`: `opacity-60 pointer-events-none`
- Uses `useA2UIDataModel` for local data model state

**index.ts** — Barrel exports: `A2UIRenderer`, `RenderComponent`, `RenderChildren`, all types, `useA2UIDataModel`.

## Phase 4: Frontend Canvas Panel + Integration

**Goal**: Create the canvas panel (iframe) and wire everything into the chat flow.

### 4.1 Canvas panel with iframe [category: code]

Target: `web/src/components/canvas/CanvasPanel.tsx`, `web/src/components/canvas/CanvasPanelHeader.tsx`, `web/src/components/canvas/hooks/useCanvasPanel.ts`

**useCanvasPanel.ts** — Follows `useArtifacts` pattern (`web/src/hooks/useArtifacts.ts`):
- localStorage key: `gobby-canvas-panel-width`
- Default width 600, constrained [400, 1200]
- Exposes: `activeCanvas`, `isPanelOpen`, `panelWidth`, `openCanvas`, `closeCanvas`, `setPanelWidth`
- `CanvasPanelState`: `{canvasId, title, url, width?, height?}`

**CanvasPanelHeader.tsx** — Follows ArtifactPanel header:
- Title (truncated), "canvas" badge, close button
- Uses existing `Button` and `Badge` components

**CanvasPanel.tsx** — Right-side panel:
- `border-l border-border bg-background shrink-0` (matches ArtifactPanel)
- Dynamic width via inline style
- Sandboxed iframe: `<iframe sandbox="allow-scripts" src={url} />`
- Reuses `ResizeHandle` from `web/src/components/chat/artifacts/ResizeHandle.tsx`

### 4.2 Frontend integration (ToolCallCard, useChat, ChatPage) [category: code] (depends: 3.1, 3.2, 4.1)

Target: `web/src/components/chat/ToolCallCard.tsx`, `web/src/hooks/useChat.ts`, `web/src/components/chat/ChatPage.tsx`, `web/src/types/chat.ts`

**useChat.ts** changes:
1. Add `'canvas_event'` to WebSocket subscription (line 248)
2. Add state: `canvasSurfaces: Map<string, A2UISurfaceState>`, `canvasPanel: CanvasPanelState | null`
3. Handle `canvas_event` messages: `surface_update` → add/update surface, `interaction_confirmed`/`close_canvas` → mark completed/clear panel, `panel_present` → set panel. Also handle `canvas_rehydrate` event to restore UI state on WebSocket reconnect.
4. Add `respondToCanvas(canvasId, action)` callback — sends `canvas_interaction` via WebSocket
5. Add to return value

**ToolCallCard.tsx** (line 225):
- Add `render_surface` detection (follows `isAskUserQuestion` pattern)
- When `render_surface` tool call detected and matching surface exists in `canvasSurfaces` map, render `A2UIRenderer` inline
- Thread `onCanvasInteraction` prop through `ToolCallCards` → `ToolCallItem` → `CanvasSurfaceCard` → `A2UIRenderer`

**ChatPage.tsx**:
- Add `useCanvasPanel` hook
- React to `chat.canvasPanel` changes → open/close canvas panel
- Add CanvasPanel + ResizeHandle to layout (right panel slot, same position as ArtifactPanel)
- Canvas and artifact panels are mutually exclusive — canvas takes priority when both active

**chat.ts** types — Add: `canvasSurfaces`, `canvasPanel`, `onCanvasInteraction` to ChatState.

## Phase 5: Agent Skill

**Goal**: Create the canvas skill with source-filtered auto-injection for web UI sessions.

### 5.1 Create canvas SKILL.md [category: code] (depends: Phase 1, 2.1)

Target: `src/gobby/install/shared/skills/canvas/SKILL.md` (new)

**Frontmatter**:
```yaml
---
name: canvas
description: Render interactive A2UI surfaces and present HTML content in the canvas panel
version: "1.0.0"
category: core
alwaysApply: true
metadata:
  gobby:
    audience: all
    sources: ["claude_sdk_web_chat"]
    priority: 40
---
```

**Skill body teaches**:
1. Two modes: inline A2UI (JSON → React) vs canvas panel (iframe)
2. A2UI component catalog — all 11 types with properties and JSON examples
3. Data binding — BoundValue literals vs JSON pointer paths
4. Actions — definition, context resolution, interaction result format
5. Copy-paste patterns — approval form, input form, status card, selection list (complete JSON, not pseudo-code)
6. Canvas panel workflow — write HTML → `canvas_present` → interact → `close_canvas`. Explicitly document that the HTML iframe is sandboxed (`allow-scripts`) and CANNOT make XHR/fetch requests to external APIs or local servers (no SPAs).
7. When to use what — AskUserQuestion vs inline A2UI vs canvas panel

### 5.2 End-to-end tests and skill verification [category: code] (depends: Phase 1, Phase 2, Phase 3, Phase 4, 5.1)

Target: `tests/skills/test_canvas_skill.py` (new)

- `test_canvas_skill_parses` — skill has correct name, alwaysApply, sources
- `test_canvas_skill_injected_for_web_ui` — source="claude_sdk_web_chat" matches
- `test_canvas_skill_not_injected_for_cli` — source="claude" does not match

## Task Dependency Graph

```
1.1 (sources) ───────────────────────────────┐
                                              │
2.1 (registry) ──▶ 2.2 (wiring)              │
                                              │
3.1 (components) ──▶ 3.2 (renderer) ──┐      │
                                       ├──▶ 4.2 (frontend integration)
4.1 (canvas panel) ────────────────────┘      │
                                              │
                              5.1 (skill) ◀───┘
                                   │
                              5.2 (e2e tests)
```

**Parallel tracks**: Tasks 1.1, 2.1, 3.1, 4.1 have no mutual dependencies.

## Task Mapping

| Plan Item | Task Ref | Status |
|-----------|----------|--------|
| 1.1 Add sources field | | pending |
| 2.1 Canvas tool registry | | pending |
| 2.2 Backend wiring | | pending |
| 3.1 A2UI types + components | | pending |
| 3.2 A2UIRenderer + hooks | | pending |
| 4.1 Canvas panel | | pending |
| 4.2 Frontend integration | | pending |
| 5.1 Canvas SKILL.md | | pending |
| 5.2 E2E tests | | pending |

## Verification

1. **Backend unit tests**: `uv run pytest tests/mcp_proxy/tools/test_canvas.py -v` — render, update, close, wait, resolve, cancel, sweep, rate limit, bounds
2. **Skill tests**: `uv run pytest tests/skills/test_canvas_skill.py -v` — parse, source injection, source rejection
3. **Source filter tests**: `uv run pytest tests/skills/test_parser.py tests/skills/test_injector.py -v -k sources`
4. **Frontend**: Vitest — A2UI renderer builds tree, BoundValue resolution, unknown type error, action callbacks, panel open/close, ErrorBoundary catches render failures.
5. **A2UI E2E**: Agent calls `render_surface` with approval form → renders inline → click Approve → agent gets result → surface dims
6. **Canvas panel E2E**: Agent writes HTML → `canvas_present` → panel opens with iframe → `close_canvas` closes
7. **Security**: Unknown A2UI types rejected, BoundValue never renders as HTML, iframe sandboxed, `canvas_present` avoids exposing workspace by managing copying directly.
8. **Skill source filtering**: Canvas skill injected for `source="claude_sdk_web_chat"`, NOT for `source="claude"` or `source="gemini"`

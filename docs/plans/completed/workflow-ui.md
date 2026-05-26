# Workflow UI: DB Storage + Visual Builder

## Context

Workflow definitions currently live as YAML files on disk, loaded by `WorkflowLoader` with filesystem scanning. This works for power users but prevents building a visual editor. This plan moves definitions into PostgreSQL as the source of truth, adds REST API routes for CRUD, builds a list page UI, and (in a future epic) a visual drag-and-drop builder using React Flow.

**User decisions:**
- Single `workflow_definitions` table for both workflows and pipelines (type discriminator)
- Full drag-and-drop canvas using React Flow (`@xyflow/react`) — Phase 4, separate epic
- One-off import of bundled YAMLs into DB (no fork mechanism — not live with users yet)
- `definition_json` is source of truth; column fields (`name`, `enabled`, `priority`, `sources`) are denormalized for querying

**Inspiration & prior art:**
- [Sim Studio](https://github.com/simstudioai/sim) — React Flow + Zustand, separation of canvas UI from execution
- [Open Agent Builder](https://github.com/firecrawl/open-agent-builder) — React Flow, 8 node types (Start, Agent, MCP Tools, Transform, If/Else, While Loop, User Approval, End), node-level property editing
- [Flowise](https://flowiseai.com/) — React Flow for LLM chain building
- [Lobster/OpenClaw](https://github.com/openclaw/lobster) — YAML pipeline engine (no visual builder), confirms our approach is differentiated
- React Flow has a [Workflow Editor template](https://reactflow.dev/components/templates/workflow-editor) with drag-and-drop sidebar, auto-layout (ELK), dark mode, runner functionality

**Key React Flow patterns** (from Context7 docs):
- `@xyflow/react` provides `ReactFlow`, `useNodesState`, `useEdgesState`, `Handle`, `Position`, `Controls`, `MiniMap`, `Background`, `Panel`
- Drag-and-drop: `onDragOver` (preventDefault + dropEffect) + `onDrop` (screenToFlowPosition + create node) + `onDragStart` on palette items (setData + effectAllowed)
- Custom nodes: `nodeTypes` object defined OUTSIDE component to prevent re-renders, each node uses `<Handle type="source|target" position={Position.Top|Bottom|Left|Right} />`
- Dagre layout: `@dagrejs/dagre` (already installed) — `dagreGraph.setGraph({rankdir: 'TB'})`, set nodes with width/height, set edges, call `dagre.layout()`, map positions
- Edges: `ConnectionLineType.SmoothStep` with `animated: true` for clean workflow arrows

## Constraints

1. **Migration 102 must be synchronous.** `WorkflowLoader.discover_workflows()` is async. The migration must scan the filesystem directly using `pathlib.Path.glob("*.yaml")` + `yaml.safe_load()`, not the loader.
2. **BASELINE_VERSION must bump to 102** and `workflow_definitions` table + indexes must be added to `BASELINE_SCHEMA`.
3. **Routes `__init__.py` must export** `create_workflows_router` alongside other router factories.
4. **`@xyflow/react` not yet installed** — must `npm install` before Phase 4. `@dagrejs/dagre` and `@atlaskit/pragmatic-drag-and-drop` already present.
5. **Workflow type mapping:** YAML `type: "pipeline"` -> DB `workflow_type: 'pipeline'`; YAML `type: "step"` or `type: "lifecycle"` or unset -> DB `workflow_type: 'workflow'`.
6. **Pipeline node unification:** `PipelineStep` has 6 mutually exclusive execution types (exec, prompt, invoke_pipeline, mcp, spawn_session, activate_workflow) — use a single `PipelineStepNode.tsx` with internal discriminator, not separate node files.

---

## Phase 1: Database Storage Layer

**Goal**: Workflow definitions stored in PostgreSQL with CRUD operations, bundled YAMLs imported.

**Tasks:**
- [ ] Add migration 102 — `workflow_definitions` table + bundled YAML import (code)
- [ ] Create `LocalWorkflowDefinitionManager` storage manager (code)
- [ ] Add DB-first lookup path to `WorkflowLoader` (code, depends: Task 1-2)

### Migration 102 — `workflow_definitions` table

**File:** `src/gobby/storage/migrations.py`

```sql
CREATE TABLE workflow_definitions (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    workflow_type TEXT NOT NULL DEFAULT 'workflow',  -- 'workflow' | 'pipeline'
    version TEXT DEFAULT '1.0',
    enabled INTEGER DEFAULT 1,
    priority INTEGER DEFAULT 100,
    sources TEXT,              -- JSON array: ["claude", "gemini"] or null
    definition_json TEXT NOT NULL,  -- Full WorkflowDefinition/PipelineDefinition as JSON
    canvas_json TEXT,          -- React Flow node/edge positions (UI-only state)
    source TEXT DEFAULT 'custom',  -- 'bundled' | 'custom' | 'imported'
    tags TEXT,                 -- JSON array for categorization
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(name, COALESCE(project_id, '__global__'))
);
CREATE INDEX idx_wf_defs_project ON workflow_definitions(project_id);
CREATE INDEX idx_wf_defs_name ON workflow_definitions(name);
CREATE INDEX idx_wf_defs_type ON workflow_definitions(workflow_type);
CREATE INDEX idx_wf_defs_enabled ON workflow_definitions(enabled);
```

Also add table + indexes to `BASELINE_SCHEMA`, bump `BASELINE_VERSION` to 102.

### Bundled Workflow Import (part of migration 102)

Python migration function `_migrate_add_workflow_definitions(db)`:
- Runs the CREATE TABLE + indexes SQL
- Scans `_BUNDLED_WORKFLOWS_DIR` (from `src/gobby/workflows/loader.py` line 53) using `Path.glob("**/*.yaml")` — **NOT** the async `WorkflowLoader` (see Constraint 1)
- For each YAML: `yaml.safe_load()`, extract `name`, `description`, `type`, `enabled`, `priority`, `sources`, `version`
- Map `type` to `workflow_type`: `"pipeline"` -> `"pipeline"`, else `"workflow"` (see Constraint 5)
- `json.dumps()` the full dict as `definition_json`, set `source='bundled'`, `project_id=NULL`
- `INSERT OR IGNORE` to handle re-runs
- Handles both root directory and `lifecycle/` subdirectory workflows

### Storage Manager

**New file:** `src/gobby/storage/workflow_definitions.py`

**Pattern:** Follow `src/gobby/storage/agent_definitions.py` exactly.

```python
@dataclass
class WorkflowDefinitionRow:
    id: str
    name: str
    workflow_type: str  # 'workflow' | 'pipeline'
    enabled: bool
    priority: int
    definition_json: str
    canvas_json: str | None
    source: str
    # Full column set (all must be present):
    project_id: str | None = None
    description: str | None = None
    version: str = "1.0"
    sources: list[str] | None = None  # parsed from JSON
    tags: list[str] | None = None     # parsed from JSON
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: psycopg.Row) -> "WorkflowDefinitionRow":
        """Convert DB row to dataclass, with JSON parsing for sources/tags."""
        ...

    def to_dict(self) -> dict[str, Any]:
        """Convert dataclass to dict (all fields)."""
        ...

class LocalWorkflowDefinitionManager:
    def __init__(self, db: HubDatabase):
        self.db = db

    def create(name, workflow_type, definition_json, ...) -> WorkflowDefinitionRow
    def get(definition_id) -> WorkflowDefinitionRow | None
    def get_by_name(name, project_id) -> WorkflowDefinitionRow | None  # project-scoped first, global fallback
    def update(definition_id, **fields) -> WorkflowDefinitionRow
    def delete(definition_id) -> bool
    def list_all(project_id=None, workflow_type=None, enabled=None) -> list[WorkflowDefinitionRow]
    def import_from_yaml(yaml_content: str, project_id=None) -> WorkflowDefinitionRow
    def export_to_yaml(definition_id) -> str
    def duplicate(definition_id, new_name) -> WorkflowDefinitionRow
```

### WorkflowLoader Integration

**File:** `src/gobby/workflows/loader.py`

- Add optional `db: HubDatabase | None = None` parameter to `__init__`
- In `load_workflow()` (line ~110): before filesystem search, if `self.db` is set, query DB via `LocalWorkflowDefinitionManager.get_by_name(name, project_path)`. If found, deserialize `definition_json` and return as `WorkflowDefinition`/`PipelineDefinition`. Cache with `updated_at` as staleness key.
- In `discover_workflows()` (line ~700): merge DB results with filesystem. DB entries shadow filesystem entries with same name.
- **File:** `src/gobby/runner.py` (line 305): pass `db=self.database` when constructing `WorkflowLoader()`

---

## Phase 2: HTTP API Routes

**Goal**: REST API for workflow definition CRUD, import/export, toggle.

**Tasks:**
- [ ] Create `src/gobby/servers/routes/workflows.py` with CRUD endpoints (code, depends: Phase 1)
- [ ] Register router in `__init__.py` and `http.py` (code, depends: Task 1)

### Request/Response Models

**New file:** `src/gobby/servers/routes/workflows.py`

**Pattern:** Follow `src/gobby/servers/routes/agents.py` exactly (factory function, Pydantic request models, lazy imports, metrics, try/catch).

```python
class CreateWorkflowRequest(BaseModel):
    name: str
    description: str | None = None
    workflow_type: str = "workflow"  # 'workflow' | 'pipeline'
    project_id: str | None = None
    definition: dict[str, Any]  # Full definition as dict
    canvas: dict[str, Any] | None = None
    enabled: bool = True
    priority: int = 100
    tags: list[str] | None = None

class UpdateWorkflowRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    definition: dict[str, Any] | None = None
    canvas: dict[str, Any] | None = None
    enabled: bool | None = None
    priority: int | None = None
    tags: list[str] | None = None
```

### Endpoints

Factory: `create_workflows_router(server: "HTTPServer") -> APIRouter`
Prefix: `/api/workflows`

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/workflows` | List all definitions (query params: type, enabled, project_id) |
| GET | `/api/workflows/{id}` | Get single definition |
| POST | `/api/workflows` | Create new definition |
| PUT | `/api/workflows/{id}` | Update definition |
| DELETE | `/api/workflows/{id}` | Delete definition |
| POST | `/api/workflows/import` | Import from YAML string |
| GET | `/api/workflows/{id}/export` | Export as YAML |
| POST | `/api/workflows/{id}/duplicate` | Clone with new name |
| PUT | `/api/workflows/{id}/toggle` | Quick enable/disable toggle |

### Registration

- **File:** `src/gobby/servers/routes/__init__.py` — add import and `__all__` entry for `create_workflows_router`
- **File:** `src/gobby/servers/http.py` — add `app.include_router(create_workflows_router(self))` in `_register_routes()` (line ~684)

---

## Phase 3: Web UI — Workflow List Page

**Goal**: Workflows tab shows all definitions with cards, filters, and CRUD actions.

**Tasks:**
- [ ] Create `useWorkflows` data hook (code, depends: Phase 2)
- [ ] Create `WorkflowsPage` list view component + CSS (code)
- [ ] Wire `WorkflowsPage` into `App.tsx` workflows tab (code)

### New Dependencies

```bash
cd web && npm install @xyflow/react
```

Note: `@dagrejs/dagre` already installed (for auto-layout).

### `useWorkflows.ts` Hook

**New file:** `web/src/hooks/useWorkflows.ts`

**Pattern:** Follow `web/src/hooks/useMcp.ts`

```typescript
export interface WorkflowSummary {
  id: string
  name: string
  description: string | null
  workflow_type: 'workflow' | 'pipeline'
  enabled: boolean
  priority: number
  source: 'bundled' | 'custom' | 'imported'
  tags: string[]
  step_count: number
  trigger_count: number
  updated_at: string
}

export function useWorkflows() {
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([])
  const [loading, setLoading] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectedWorkflow, setSelectedWorkflow] = useState<WorkflowDetail | null>(null)

  // CRUD operations
  const fetchWorkflows: () => Promise<void>
  const fetchWorkflow: (id: string) => Promise<WorkflowDetail>
  const createWorkflow: (data: CreateWorkflowData) => Promise<string | null>
  const updateWorkflow: (id: string, data: UpdateWorkflowData) => Promise<boolean>
  const deleteWorkflow: (id: string) => Promise<boolean>
  const duplicateWorkflow: (id: string, newName: string) => Promise<string | null>
  const toggleEnabled: (id: string) => Promise<boolean>
  const importYaml: (yaml: string) => Promise<string | null>
  const exportYaml: (id: string) => Promise<string | null>
}
```

### `WorkflowsPage.tsx` — List View

**New files:** `web/src/components/WorkflowsPage.tsx`, `web/src/components/WorkflowsPage.css`

**Pattern:** Follow `McpPage.tsx` layout.

```
WorkflowsPage
├── Toolbar
│   ├── Title + count badge
│   ├── Search input
│   ├── Import button
│   ├── + New Workflow button
│   └── + New Pipeline button
├── Overview Cards (clickable filters)
│   ├── Total count
│   ├── Workflows count
│   ├── Pipelines count
│   └── Active/Enabled count
├── Filter Chips
│   ├── Type: All | Workflows | Pipelines
│   ├── Source: All | Bundled | Custom | Imported
│   └── Status: All | Enabled | Disabled
├── Workflow Cards Grid
│   └── WorkflowCard (each)
│       ├── Name + type badge (workflow/pipeline)
│       ├── Description excerpt
│       ├── Badges: source, priority, step count, trigger count
│       ├── Enabled toggle switch
│       └── Actions: Edit (opens builder), Duplicate, Export, Delete
└── Create Modal (for new workflow/pipeline scaffold)
```

Clicking "Edit" on a card transitions to the builder view. Use local state (`view: 'list' | 'builder'`) to switch between list and builder within the same page. Until Phase 4 is implemented, Edit opens a read-only JSON view of the definition.

### `App.tsx` Integration

**File:** `web/src/App.tsx`

Replace the `ComingSoonPage` fallthrough for `activeTab === 'workflows'` with `<WorkflowsPage />` (around line 309).

---

## Phase 4: Web UI — Visual Workflow Builder (Separate Epic)

**Goal**: React Flow canvas for visual workflow editing with drag-and-drop and property panel.

This phase is large enough for its own epic. Broken into sub-phases below.

### 4A: Canvas Scaffold

**Tasks:**
- [ ] Create `WorkflowBuilder` component with React Flow canvas, toolbar, and palette sidebar
- [ ] Implement dagre auto-layout for positioning nodes without saved canvas state
- [ ] Implement `definitionToFlow()` and `flowToDefinition()` serialization functions

#### React Flow Canvas Architecture

```
WorkflowBuilder
├── Toolbar
│   ├── Back button (return to list)
│   ├── Workflow name (editable)
│   ├── Type badge
│   ├── Save button
│   ├── Export YAML button
│   ├── Run/Test button (pipelines)
│   └── Settings gear (priority, enabled, sources, variables)
├── Left Sidebar — Node Palette (200px)
│   ├── For Workflows:
│   │   ├── Step (draggable)
│   │   ├── Trigger Group (draggable)
│   │   ├── Observer (draggable)
│   │   └── Exit Condition (draggable)
│   ├── For Pipelines:
│   │   ├── Exec Step (draggable)
│   │   ├── Prompt Step (draggable)
│   │   ├── MCP Step (draggable)
│   │   ├── Pipeline Step (draggable)
│   │   ├── Spawn Session (draggable)
│   │   └── Approval Gate (draggable)
│   └── Common:
│       ├── Variable (draggable)
│       └── Rule (draggable)
├── Center — React Flow Canvas
│   ├── Custom nodes (see below)
│   ├── Edges with labels (transition conditions)
│   ├── Minimap
│   ├── Controls (zoom, fit, lock)
│   └── Background (dots pattern)
└── Right Panel — Property Editor (360px)
    ├── Dynamic form based on selected node type
    ├── Step properties: name, tools, rules
    ├── Transition editor with CodeMirror for `when` expressions
    ├── Trigger editor: event type, conditions, actions
    ├── Action list editor (add/remove/reorder)
    └── Variable declarations editor
```

#### Serialization: Canvas <-> Definition

**Definition -> Canvas (on load):**

```typescript
function definitionToFlow(def: WorkflowDefinition, canvas?: CanvasState): { nodes: Node[], edges: Edge[] }
```

- Each step -> StepNode (positioned from canvas_json or auto-layout via dagre)
- Each trigger group -> TriggerNode
- Each observer -> ObserverNode
- Transitions -> edges between StepNodes
- Auto-layout with `@dagrejs/dagre` when no canvas positions saved

**Canvas -> Definition (on save):**

```typescript
function flowToDefinition(nodes: Node[], edges: Edge[]): { definition: WorkflowDefinition, canvas: CanvasState }
```

- StepNodes -> steps array (preserving all properties from the property editor)
- TriggerNodes -> triggers dict
- Edges -> transitions on source steps
- Canvas positions saved separately in `canvas_json`

#### Drag-and-Drop from Palette

Using React Flow's HTML Drag and Drop API integration:

**Palette sidebar** — each item is a draggable div:
```tsx
const onDragStart = (event: DragEvent, nodeType: string) => {
  event.dataTransfer.setData('application/reactflow', nodeType)
  event.dataTransfer.effectAllowed = 'move'
}
// <div draggable onDragStart={(e) => onDragStart(e, 'stepNode')}>Step</div>
```

**Canvas** — `onDragOver` + `onDrop` handlers on `<ReactFlow>`:
```tsx
const onDragOver = useCallback((event) => {
  event.preventDefault()
  event.dataTransfer.dropEffect = 'move'
}, [])

const onDrop = useCallback((event) => {
  event.preventDefault()
  const type = event.dataTransfer.getData('application/reactflow')
  if (!type) return
  const position = screenToFlowPosition({ x: event.clientX, y: event.clientY })
  const newNode = { id: getId(), type, position, data: getDefaultData(type) }
  setNodes((nds) => nds.concat(newNode))
  setSelectedNode(newNode.id) // opens property panel
}, [screenToFlowPosition])
```

**Auto-layout** with dagre (already installed) when no canvas positions:
```tsx
const getLayoutedElements = (nodes, edges, direction = 'TB') => {
  const g = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}))
  g.setGraph({ rankdir: direction })
  nodes.forEach(n => g.setNode(n.id, { width: 280, height: 80 }))
  edges.forEach(e => g.setEdge(e.source, e.target))
  dagre.layout(g)
  return { nodes: nodes.map(n => ({ ...n, position: { x: g.node(n.id).x - 140, y: g.node(n.id).y - 40 } })), edges }
}
```

**Node type registration** (defined OUTSIDE component to prevent re-renders):
```tsx
const nodeTypes = {
  stepNode: StepNode,
  triggerNode: TriggerNode,
  observerNode: ObserverNode,
  variableNode: VariableNode,
  exitNode: ExitNode,
  pipelineStepNode: PipelineStepNode,  // unified: exec/prompt/mcp/spawn/pipeline/activate
}
```

### 4B: Custom Node Types

**Tasks:**
- [ ] Create shared node types file (`workflow-nodes/nodeTypes.ts`) with type registry and defaults
- [ ] Create `StepNode` component for workflow steps
- [ ] Create `TriggerNode` component for trigger groups
- [ ] Create `ObserverNode` component for observers
- [ ] Create `PipelineStepNode` component (unified: exec/prompt/mcp/spawn/pipeline/activate)
- [ ] Create `VariableNode` and `ExitNode` utility nodes

**Workflow nodes** (`web/src/components/workflow-nodes/`):

| Node Type | File | Visual |
|-----------|------|--------|
| `StepNode` | `StepNode.tsx` | Card with name header, tool restriction badges, rule count, transition handles on bottom |
| `TriggerNode` | `TriggerNode.tsx` | Colored header (event type), condition preview, action count |
| `ObserverNode` | `ObserverNode.tsx` | Eye icon, event type, variable assignments |
| `VariableNode` | `VariableNode.tsx` | Variable icon, name/default pairs, scope badge (workflow/session) |
| `ExitNode` | `ExitNode.tsx` | Flag icon, exit condition expression |
| `PipelineStepNode` | `PipelineStepNode.tsx` | Unified: icon varies by exec type (terminal/message/plug/etc), type badge, preview |

Each node:
- Has connection handles (source/target) for edge connections
- Shows a compact preview in the canvas
- Opens full property editor in right panel on click/select
- Matches the dark theme (--bg-secondary cards, --border edges, --accent highlights)

### Edge Types

- **Transition edges** (workflow): Labeled with `when` condition, colored by type
- **Sequential edges** (pipeline): Simple arrow showing execution order
- **Conditional edges**: Dashed line with condition label

### 4C: Property Panel

**Tasks:**
- [ ] Create `WorkflowPropertyPanel` right-side panel shell with dynamic form routing
- [ ] Implement step properties form (name, description, tools, rules, transitions)
- [ ] Implement trigger properties form (event type, conditions, actions)
- [ ] Implement pipeline step properties form (type-specific: exec, prompt, mcp)
- [ ] Add CodeMirror integration for `when` expressions and command editing

Dynamic form rendered based on selected node type. Uses the same form patterns as `AgentDefinitionsPage.tsx` (grid layout, labeled inputs, collapsible sections).

**Step properties form:**
- Name (text input)
- Description (textarea)
- Status message (text input with template preview)
- Tool Restrictions section (collapsible):
  - Allowed tools (multi-select or "all")
  - Blocked tools (multi-select)
  - MCP tool restrictions (server:tool format)
- Rules section (collapsible):
  - Inline rules list (add/remove, each with when/tool/decision/reason)
  - Named rule references (check_rules)
- Actions section (collapsible):
  - on_enter actions list
  - on_exit actions list
- Transitions section (collapsible):
  - List of transitions with target step dropdown + when condition (CodeMirror)

**Trigger properties form:**
- Event type (select: on_session_start, on_stop, on_before_tool, on_before_agent, etc.)
- Actions list with condition (when) and action type selectors

**Pipeline step properties form:**
- Step type (read-only badge)
- Type-specific fields:
  - Exec: command (CodeMirror), timeout
  - Prompt: template (CodeMirror), tools list
  - MCP: server select, tool select, arguments editor
- Condition (CodeMirror)
- Approval gate toggle + config

### 4D: Polish

**Tasks:**
- [ ] Implement save/load cycle (canvas_json persistence)
- [ ] Add edge type styling (transition vs sequential vs conditional)
- [ ] Add workflow-level settings panel (gear icon)

#### Settings Panel (Workflow-Level)

Accessible from the gear icon in the builder toolbar:
- Name, description (editable)
- Enabled toggle
- Priority (number input)
- Sources filter (multi-select: claude, gemini, codex)
- Variables editor (key/value table for workflow-scoped)
- Session variables editor (key/value table for session-scoped)
- Rule definitions editor (named rules for reuse within workflow)
- Exit condition (CodeMirror expression)

---

## Phase 5: Integration & Polish (Follow-up)

**Goal**: Update MCP tools to read from DB, add templates.

**Tasks:**
- [ ] Update MCP workflow query tools to use DB with filesystem fallback (code, depends: Phase 1)
- [ ] Create workflow templates (code)

### MCP Tools Update

**File:** `src/gobby/mcp_proxy/tools/workflows/_query.py`

`list_workflows()` (line 99): query DB via `LocalWorkflowDefinitionManager.list_all()`, merge with filesystem. `get_workflow()` (line 25): handled automatically by WorkflowLoader DB integration from Phase 1. Keep backward compatibility — if DB has no results, fall back to loader.

### Workflow Templates

Pre-built templates accessible from the "New" button:
- **Blank Workflow** — empty steps + triggers
- **Lifecycle Template** — enabled:true, common triggers (session_start, stop, before_tool)
- **TDD Developer** — red/green/blue steps with tool restrictions
- **Blank Pipeline** — empty sequential pipeline
- **CI Pipeline Template** — build/test/deploy steps with approval gate

---

## File Manifest

### New Files (Phases 1-3)

| File | Purpose |
|------|---------|
| `src/gobby/storage/workflow_definitions.py` | `WorkflowDefinitionRow` + `LocalWorkflowDefinitionManager` |
| `src/gobby/servers/routes/workflows.py` | HTTP API routes for workflow CRUD |
| `web/src/hooks/useWorkflows.ts` | React hook for workflow API |
| `web/src/components/WorkflowsPage.tsx` | List view page |
| `web/src/components/WorkflowsPage.css` | List view styles |

### New Files (Phase 4, separate epic)

| File | Purpose |
|------|---------|
| `web/src/components/WorkflowBuilder.tsx` | Canvas builder page |
| `web/src/components/WorkflowBuilder.css` | Builder styles |
| `web/src/components/WorkflowPropertyPanel.tsx` | Right-side property editor |
| `web/src/components/workflow-nodes/nodeTypes.ts` | Node type registry + shared types + defaults |
| `web/src/components/workflow-nodes/StepNode.tsx` | Workflow step node |
| `web/src/components/workflow-nodes/TriggerNode.tsx` | Trigger group node |
| `web/src/components/workflow-nodes/ObserverNode.tsx` | Observer node |
| `web/src/components/workflow-nodes/PipelineStepNode.tsx` | Unified pipeline step node (all execution types) |
| `web/src/components/workflow-nodes/VariableNode.tsx` | Variable declarations node |
| `web/src/components/workflow-nodes/ExitNode.tsx` | Exit condition node |

### Modified Files (Phases 1-3)

| File | Change |
|------|--------|
| `src/gobby/storage/migrations.py` | Migration 102 + bump BASELINE_VERSION + add to BASELINE_SCHEMA |
| `src/gobby/workflows/loader.py` | Add `db` param to `__init__`, DB-first in `load_workflow`/`discover_workflows` |
| `src/gobby/runner.py` | Pass `db=self.database` to `WorkflowLoader()` (line 305) |
| `src/gobby/servers/routes/__init__.py` | Export `create_workflows_router` |
| `src/gobby/servers/http.py` | Register workflows router in `_register_routes()` |
| `web/src/App.tsx` | Wire `WorkflowsPage` to 'workflows' tab |
| `web/package.json` | Add `@xyflow/react` dependency |

### Critical Reference Files

| File | Why |
|------|-----|
| `src/gobby/storage/agent_definitions.py` | Storage manager pattern to follow |
| `src/gobby/servers/routes/agents.py` | HTTP route pattern to follow |
| `web/src/hooks/useMcp.ts` | Data hook pattern to follow |
| `web/src/components/McpPage.tsx` | List page pattern to follow |
| `src/gobby/workflows/definitions.py` | `WorkflowDefinition` + `PipelineDefinition` Pydantic models (definition_json deserializes to these) |

---

## Implementation Order

1. **DB layer** — migration, storage manager
2. **HTTP API** — routes, registration, test with curl
3. **useWorkflows hook** — API integration
4. **WorkflowsPage list view** — cards, filters, CRUD
5. **WorkflowLoader integration** — DB-first loading
6. *(Separate epic)* **WorkflowBuilder canvas** — React Flow setup, node types, palette
7. *(Separate epic)* **Property panel** — forms for each node type
8. *(Separate epic)* **Serialization** — definition <-> canvas conversion
9. **Templates + polish** — starter templates, import/export

---

## Verification

1. **DB migration**: `uv run gobby restart` — verify table created, bundled workflows imported
2. **API**: `curl localhost:60887/api/workflows` — verify list returns imported workflows
3. **API CRUD**: POST create, PUT update, DELETE, GET single — all work
4. **UI list**: Open web UI -> Workflows tab -> see all imported workflows with cards
5. **UI builder**: Click Edit on a workflow -> canvas shows steps/triggers as nodes
6. **UI edit**: Drag new step from palette -> edit properties -> save -> verify API persists
7. **UI pipeline**: Create pipeline -> add exec/prompt/mcp steps -> save -> verify
8. **Import/Export**: Export workflow as YAML -> import YAML -> verify round-trip
9. **Loader integration**: Verify workflow engine still loads definitions correctly (via DB)

## Plan Verification (TDD Compliance)

- No explicit test tasks found (TDD applied automatically by /gobby:expand)
- Dependency tree is valid (no cycles, all refs exist: Phase 2 depends Phase 1, Phase 3 depends Phase 2)
- Categories assigned: all `code` except npm install (`config`)

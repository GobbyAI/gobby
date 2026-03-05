# Graph-RAG Codebase Indexing for Gobby

## Context

Gobby agents currently spend 50-100K tokens per session exploring codebases with Glob/Grep/Read. This is wasteful — the same structural knowledge gets rediscovered every session. By pre-indexing repos into a searchable code graph (Neo4j) + vector store (Qdrant), agents can retrieve relevant code context in a single tool call instead of 20+ exploration calls.

Gobby already has Qdrant (VectorStore class) and Neo4j (Neo4jClient via HTTP API) wired up for the memory system. This feature extends both to handle code-specific data with separate collections/labels.

**Goal**: A `gobby-codebase` MCP server that provides Graph-RAG over the current project's repository, saving tokens and improving agent code understanding.

---

## Architecture

### Retrieval Flow
```
query "how does task validation work?"
  → embed query
  → Qdrant search (code_chunks collection, top-20)
  → for each hit, Neo4j expand (callers/callees/imports, 1-2 hops)
  → deduplicate + re-rank expanded set
  → return top-k code snippets with graph context
```

### Data Flow (Indexing)
```
git repo files
  → filter by language (extension map)
  → tree-sitter parse → extract symbols (functions, classes, methods, imports)
  → chunk at symbol boundaries (include docstrings, decorators, type hints)
  → embed chunks via LiteLLM
  → upsert to Qdrant (code_chunks collection, payload: file, symbol, kind, lines)
  → build graph in Neo4j (CodeFile/CodeClass/CodeFunction nodes, CALLS/IMPORTS/INHERITS/CONTAINS edges)
  → track file hashes in SQLite for incremental re-indexing
```

---

## Module Structure

```
src/gobby/codebase/
├── __init__.py
├── models.py          # CodeSymbol, CodeChunk, CodeEdge, IndexJob dataclasses
├── languages.py       # Language registry: extension→grammar map, tree-sitter setup
├── parser.py          # Tree-sitter parsing → CodeSymbol extraction per language
├── chunker.py         # Symbol→embeddable chunk conversion with context
├── graph.py           # Neo4j code graph builder (nodes + edges)
├── indexer.py         # Orchestrator: parse→chunk→embed→store, incremental logic
└── retriever.py       # Graph-RAG retrieval: Qdrant search → Neo4j expand → re-rank

src/gobby/storage/codebase.py          # SQLite tables for indexing state
src/gobby/config/codebase.py           # CodebaseConfig pydantic model
src/gobby/mcp_proxy/tools/codebase.py  # gobby-codebase MCP tool registry
```

---

## Key Components

### 1. Language Registry (`languages.py`)

Maps file extensions to tree-sitter grammars. Gracefully handles missing grammars.

```python
LANGUAGE_MAP = {
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp",
    ".cs": "c_sharp",
}
```

Each grammar loaded lazily on first use. If `tree-sitter-{lang}` isn't installed, that language is skipped with a warning.

**Dependencies** (pyproject.toml, required — core feature):
- `tree-sitter>=0.23`
- `tree-sitter-python`, `tree-sitter-typescript`, `tree-sitter-javascript`
- `tree-sitter-go`, `tree-sitter-rust`
- Additional grammars (ruby, java, c, cpp, c_sharp) installable by user via pip

### 2. Parser (`parser.py`)

Per-language tree-sitter queries to extract symbols:

- **Functions**: name, params, return type, docstring, decorators, body span
- **Classes**: name, bases, docstring, decorators, body span
- **Methods**: same as functions, with parent class reference
- **Imports**: module path, imported names, aliases
- **Module-level**: top-level assignments, constants

Each language gets a `LanguageParser` with tree-sitter queries tailored to its AST structure. Start with Python and TypeScript parsers, add others incrementally.

**Output**: `list[CodeSymbol]` per file.

### 3. Chunker (`chunker.py`)

Converts `CodeSymbol` → `CodeChunk` (embeddable text):

- **Symbol chunk**: signature + docstring + body (truncated at `chunk_max_lines`, default 50)
- **Context prefix**: file path + module path + class context (if method)
- **Overlap**: for large functions, create overlapping chunks with sliding window

Format for embedding:
```
# File: src/gobby/tasks/validation.py
# Module: gobby.tasks.validation
# Class: TaskValidator

def validate_task(self, task: Task, session_id: str) -> ValidationResult:
    """Validate a task meets all requirements before closing."""
    ...
```

### 4. Code Graph (`graph.py`)

Neo4j graph structure using labels to separate from memory graph:

**Node labels** (all prefixed to avoid collision with memory entities):
- `CodeFile` — properties: path, language, project_id, content_hash
- `CodeModule` — properties: qualified_name, project_id
- `CodeClass` — properties: name, qualified_name, file_path, line_start, line_end
- `CodeFunction` — properties: name, qualified_name, file_path, line_start, line_end, kind (function/method)
- `_CodeSymbol` — union label for vector index (like `_Entity` for memories)

**Edge types**:
- `CONTAINS` — File→Class, File→Function, Class→Method, Module→File
- `CALLS` — Function→Function (extracted from AST call expressions)
- `IMPORTS` — File→File or File→Module (from import statements)
- `INHERITS` — Class→Class (from base classes)

**Vector index**: `code_embedding_index` on `_CodeSymbol` nodes, separate from `entity_embedding_index`.

**Reuses**: existing `Neo4jClient.merge_node()`, `merge_relationship()`, `set_node_vector()`, `ensure_vector_index()`, `vector_search()`.

### 5. Indexer (`indexer.py`)

Orchestrates the full indexing pipeline:

```python
class CodebaseIndexer:
    async def index_project(self, project_path: str, project_id: str, full: bool = False) -> IndexJob:
        """Index a project. Incremental by default."""
        # 1. Walk files, filter by language
        # 2. Check content hashes against SQLite (skip unchanged if not full)
        # 3. For each changed file:
        #    a. Parse with tree-sitter → symbols
        #    b. Chunk symbols → embeddable text
        #    c. Embed via LiteLLM
        #    d. Upsert to Qdrant (code_chunks collection)
        #    e. Build/update Neo4j graph nodes + edges
        #    f. Update file hash in SQLite
        # 4. Clean up removed files (delete from Qdrant + Neo4j)
        # 5. Return IndexJob with stats
```

**Incremental strategy**: SHA-256 hash of file contents stored in `code_index_files`. On re-index, only process files where hash changed. Deleted files get their symbols removed from Qdrant + Neo4j.

**Respects .gitignore**: Use `git ls-files` to get tracked files only (or pathspec filtering).

### 6. Retriever (`retriever.py`)

Graph-RAG retrieval pipeline:

```python
class CodebaseRetriever:
    async def search(self, query: str, project_id: str, limit: int = 10) -> list[CodeSearchResult]:
        """Graph-RAG code search."""
        # 1. Embed query
        # 2. Qdrant search on code_chunks collection (top-k * 2 for expansion headroom)
        # 3. Get symbol IDs from Qdrant payloads
        # 4. Neo4j: expand each symbol 1-2 hops (callers, callees, imports)
        # 5. Fetch expanded symbols' code from Qdrant payloads
        # 6. RRF merge (direct hits + graph-expanded hits)
        # 7. Return top-k CodeSearchResult with code + graph context

    async def get_symbol(self, qualified_name: str) -> CodeSymbolDetail | None:
        """Get full details for a specific symbol."""

    async def get_callers(self, qualified_name: str, depth: int = 1) -> list[str]:
        """Get functions/methods that call this symbol."""

    async def get_dependencies(self, file_path: str) -> dict[str, list[str]]:
        """Get import graph for a file."""
```

**CodeSearchResult** shape (what agents see):
```python
@dataclass
class CodeSearchResult:
    symbol: str           # "TaskValidator.validate_task"
    kind: str             # "method"
    file: str             # "src/gobby/tasks/validation.py"
    lines: tuple[int,int] # (45, 82)
    code: str             # The actual source code
    score: float          # Relevance score
    graph_context: dict   # {"callers": [...], "callees": [...], "imports": [...]}
```

### 7. Storage (`storage/codebase.py`)

SQLite tables (added via migration):

```sql
CREATE TABLE code_index_files (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    language TEXT,
    symbol_count INTEGER DEFAULT 0,
    last_indexed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(project_id, file_path)
);

CREATE TABLE code_index_jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    status TEXT DEFAULT 'running',  -- running, completed, failed
    files_total INTEGER DEFAULT 0,
    files_processed INTEGER DEFAULT 0,
    files_skipped INTEGER DEFAULT 0,
    symbols_indexed INTEGER DEFAULT 0,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    error TEXT
);
```

No need for a `code_symbols` table in SQLite — symbols live in Qdrant (with payloads) and Neo4j (as nodes). SQLite just tracks indexing state.

### 8. Config (`config/codebase.py`)

```python
class CodebaseConfig(BaseModel):
    enabled: bool = True
    qdrant_collection: str = "code_chunks"
    embedding_dim: int = 1536
    chunk_max_lines: int = 50
    auto_index_on_git: bool = True
    exclude_patterns: list[str] = ["*.min.js", "*.generated.*", "vendor/", "node_modules/"]
    max_file_size_kb: int = 500  # Skip files larger than this
```

### 9. MCP Tools (`mcp_proxy/tools/codebase.py`)

Registry: `gobby-codebase` with description "Codebase indexing and Graph-RAG search"

| Tool | Description |
|------|-------------|
| `index_project` | Trigger indexing (incremental by default, `full=true` to rebuild) |
| `search_code` | Graph-RAG semantic search. Returns code + graph context. |
| `get_symbol` | Get details for a specific symbol by qualified name |
| `get_callers` | Get functions that call a given symbol |
| `get_callees` | Get functions called by a given symbol |
| `get_file_symbols` | List all symbols in a file |
| `get_dependencies` | Get import graph for a file |
| `index_status` | Check indexing progress and stats |

### 10. Registration (`registries.py`)

Add to `setup_internal_registries()`:
- New parameter: `codebase_indexer` and `codebase_retriever` (or a combined manager)
- Conditional: only register if codebase config is enabled and Qdrant + Neo4j are available
- Import: `from gobby.mcp_proxy.tools.codebase import create_codebase_registry`

### 11. GobbyRunner Wiring (`runner.py`)

- Create a second `VectorStore` instance with `collection_name="code_chunks"`
- Create `CodebaseIndexer` and `CodebaseRetriever` with shared Neo4jClient + new VectorStore
- Pass to `setup_internal_registries()`
- On startup: optionally trigger incremental re-index if `auto_index_on_git` is enabled

### 12. Git Hook Integration

- Add post-commit and post-checkout hooks that hit a daemon endpoint to trigger incremental re-index
- Use existing hook infrastructure in `src/gobby/hooks/`
- Endpoint: `POST /api/codebase/reindex` (or internal event)

### 13. Token-Saving Enforcement

Create an `inject_context` rule template that adds to session instructions:

> Before exploring the codebase with Glob, Grep, or Read, check if `search_code` on `gobby-codebase` can answer your question. The code index contains pre-parsed symbols, relationships, and semantic search over the entire repository.

This is a rule *template* (disabled by default, installable via rules engine).

---

## Implementation Order

### Phase 1: Core Infrastructure
1. `src/gobby/codebase/models.py` — Data models
2. `src/gobby/codebase/languages.py` — Language registry + tree-sitter setup
3. `src/gobby/codebase/parser.py` — Tree-sitter parsing (Python + TS first)
4. `src/gobby/codebase/chunker.py` — Symbol chunking
5. `src/gobby/config/codebase.py` — Config model
6. `src/gobby/storage/codebase.py` — SQLite storage + migration
7. `pyproject.toml` — Add tree-sitter dependencies (required)

### Phase 2: Indexing Pipeline
8. `src/gobby/codebase/graph.py` — Neo4j code graph builder
9. `src/gobby/codebase/indexer.py` — Full indexing orchestrator
10. Tests for parsing, chunking, and indexing

### Phase 3: Retrieval + MCP
11. `src/gobby/codebase/retriever.py` — Graph-RAG retrieval
12. `src/gobby/mcp_proxy/tools/codebase.py` — Tool registry
13. `src/gobby/mcp_proxy/registries.py` — Wire into setup
14. `src/gobby/runner.py` — Wire into daemon startup
15. Tests for retrieval and MCP tools

### Phase 4: Backend API
16. `src/gobby/servers/routes/codebase.py` — HTTP API routes
17. Register routes in `src/gobby/servers/http.py`

### Phase 5: Web UI
18. `web/src/hooks/useCodebase.ts` — Data hook
19. `web/src/components/codebase/CodebaseView.tsx` — Container with mode toggle
20. `web/src/components/codebase/CodeSearchView.tsx` — Search results
21. `web/src/components/codebase/CodeGraphView.tsx` — 2D graph
22. `web/src/components/codebase/SymbolDetail.tsx` — Detail panel
23. `web/src/components/codebase/IndexStatusBar.tsx` — Index controls
24. `web/src/components/projects/ProjectDetailView.tsx` — Remove tasks/sessions tabs, wire CodebaseView into Code tab

### Phase 6: Automation
25. Git hook integration for auto-reindex
26. Rule template for token-saving enforcement
27. Additional language parsers (Go, Rust, etc.)
28. Optional: 3D graph view

---

## Key Files to Modify

| File | Change |
|------|--------|
| `pyproject.toml` | Add tree-sitter deps (required) |
| `src/gobby/config/app.py` | Add `codebase: CodebaseConfig` field to DaemonConfig |
| `src/gobby/storage/migrations.py` | Add migration for code_index_files, code_index_jobs tables |
| `src/gobby/mcp_proxy/registries.py` | Register gobby-codebase in setup_internal_registries() |
| `src/gobby/runner.py` | Initialize VectorStore("code_chunks"), CodebaseIndexer, CodebaseRetriever |
| `src/gobby/servers/http.py` | Register codebase routes |
| `web/src/components/projects/ProjectDetailView.tsx` | Remove tasks/sessions tabs, wire CodebaseView |

## Reusable Components (no changes needed)

| Component | Location | Reuse |
|-----------|----------|-------|
| `VectorStore` | `src/gobby/memory/vectorstore.py` | New instance with collection_name="code_chunks" |
| `Neo4jClient` | `src/gobby/memory/neo4j_client.py` | Same instance, different labels |
| `generate_embeddings()` | `src/gobby/search/embeddings.py` | Same embedding pipeline |
| `InternalToolRegistry` | `src/gobby/mcp_proxy/tools/internal.py` | Standard MCP registration |
| RRF merging | `src/gobby/search/unified.py` | Same merging algorithm |

---

## Web UI Integration

### Overview

The project detail view (`ProjectDetailView.tsx`) already has a "Code" tab. Rather than adding a sidebar tab, we enhance the existing Code tab with three view modes: **Browse** (existing `FilesPage`), **Search** (graph-RAG code search), and **Graph** (force-graph visualization). An index status indicator shows indexing state.

We also **remove the Tasks and Sessions tabs** from the project detail view — those already have dedicated top-level sidebar tabs and are redundant here.

### ProjectDetailView Tab Changes

Current tabs: `overview | code | tasks | sessions | settings`
New tabs: `overview | code | settings`

```typescript
// ProjectDetailView.tsx
export type ProjectSubTab = 'overview' | 'code' | 'settings'

const TABS = [
  { key: 'overview', label: 'Overview' },
  { key: 'code', label: 'Code' },
  { key: 'settings', label: 'Settings' },
]
```

The Code tab switches from rendering `FilesPage` directly to rendering `CodebaseView`, which wraps `FilesPage` as its "Browse" mode:

```typescript
// Code tab render changes from:
<FilesPage projectPath={project.path} />
// To:
<CodebaseView project={project} />
```

### Location in UI

```
Projects -> Select Project -> Code tab
  +-- [Browse] [Search] [Graph]  <-- mode toggle (sub-nav bar)
  +-- Index status indicator     <-- top-right: "Indexed 2m ago - 342 files - 4,891 symbols"
  +-- Active view content
```

### Frontend Files

```
web/src/components/codebase/
+-- CodebaseView.tsx            # Container: mode toggle + dispatches to sub-views
+-- CodeSearchView.tsx          # Search results with syntax-highlighted snippets
+-- CodeGraphView.tsx           # 2D force graph (react-force-graph-2d)
+-- SymbolDetail.tsx            # Slide-out panel: symbol code + callers/callees/imports
+-- IndexStatusBar.tsx          # Compact status indicator + re-index button
+-- CodeFilters.tsx             # Filter chips: language, symbol kind, file path prefix

web/src/hooks/
+-- useCodebase.ts              # Data fetching hook (follows useMemory.ts pattern)
```

### Backend Routes

New route group at `/api/codebase/` (file: `src/gobby/servers/routes/codebase.py`):

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/codebase/search` | GET | Search code (`q`, `project_id`, `limit`, `language`, `kind`) |
| `/api/codebase/graph` | GET | Code graph for visualization (`project_id`, `limit`) |
| `/api/codebase/graph/symbol/{name}/neighbors` | GET | Symbol neighborhood expansion |
| `/api/codebase/symbols/{qualified_name}` | GET | Symbol detail (code + graph context) |
| `/api/codebase/index` | POST | Trigger indexing (`project_id`, `full`) |
| `/api/codebase/index/status` | GET | Current indexing job status |
| `/api/codebase/stats` | GET | Index statistics (files, symbols, languages) |

### UI Views

**1. Browse Mode (default)** — existing `FilesPage`, unchanged

**2. Search Mode**
- Search bar at top (debounced 300ms)
- Results: symbol name + kind badge, file path, line range, relevance score
- Each result has syntax-highlighted code snippet (CodeMirror read-only)
- Click result -> SymbolDetail slide-out with full code + graph context
- Filter bar: language chips, kind chips (function/class/method), file path prefix

**3. Graph Mode**
- Force-directed graph using react-force-graph-2d (same lib as MemoryGraph)
- Nodes color-coded: File=green, Class=purple, Function=blue, Method=cyan, Module=gray
- Edges styled by type: CALLS=solid arrow, IMPORTS=dashed, INHERITS=thick, CONTAINS=dotted
- Click node -> SymbolDetail slide-out
- Hover -> tooltip with symbol name + file path

**4. Index Status Bar** (visible in all modes)
- Compact strip: last indexed timestamp, file count, symbol count
- "Re-index" button (incremental), "Full Re-index" (rebuild)
- Progress bar during active indexing

### Data Hook (`useCodebase.ts`)

```typescript
export function useCodebase(projectId: string) {
  const [searchResults, setSearchResults] = useState<CodeSearchResult[]>([])
  const [graphData, setGraphData] = useState<GraphData | null>(null)
  const [indexStatus, setIndexStatus] = useState<IndexStatus | null>(null)
  const [stats, setStats] = useState<CodebaseStats | null>(null)

  const searchCode = async (query: string, filters?: CodeFilters) => { ... }
  const fetchGraph = async (limit?: number) => { ... }
  const fetchSymbolNeighbors = async (name: string) => { ... }
  const triggerIndex = async (full?: boolean) => { ... }
  const fetchStatus = async () => { ... }

  return { searchResults, graphData, indexStatus, stats, searchCode, triggerIndex, ... }
}
```

---

## Verification

1. **Unit tests**: Parser extracts correct symbols from Python/TS test files
2. **Unit tests**: Chunker produces well-formed embeddable text
3. **Integration test**: Index a small test repo → verify Qdrant has points, Neo4j has graph
4. **Integration test**: search_code returns relevant results with graph context
5. **E2E**: Use MCP tools (index_project → search_code) via gobby-codebase
6. **Manual**: Index gobby itself, search for "how does task validation work", verify quality of results

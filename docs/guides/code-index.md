# Code Index

AST-based symbol indexing for your codebase via the `gobby-code` MCP server. Parse source files with tree-sitter, extract symbols (functions, classes, methods, types), and retrieve them by ID instead of reading entire files — saving 90%+ tokens.

## Quick Start

Index a project and search for symbols:

```bash
# CLI: Index current project
gobby index

# CLI: Check status
gobby index status

# CLI: Force full re-index
gobby index --full

# CLI: Clear index
gobby index invalidate
```

```python
# MCP: Index status
call_tool("gobby-code", "list_indexed", {})

# MCP: Search for a symbol
call_tool("gobby-code", "search_symbols", {
    "query": "parse_config"
})

# MCP: Retrieve a specific symbol by ID
call_tool("gobby-code", "get_symbol", {
    "symbol_id": "a1b2c3d4-..."
})
```

## How It Works

### Indexing Pipeline

```
Source files
    ↓
Language detection (extension → language)
    ↓
Security checks (path traversal, symlinks, secrets, binary, size)
    ↓
Tree-sitter parsing (AST → symbols, imports, calls)
    ↓
Storage layers:
  ├→ SQLite (always) — symbol metadata, file hashes
  ├→ Qdrant (optional) — semantic embeddings
  ├→ Neo4j (optional) — call/import graph
  └→ Claude Haiku (optional) — one-line summaries
```

### Incremental Re-Indexing

Files are hashed with SHA-256. On each indexing pass, only files whose hash has changed are re-parsed. This makes re-indexing fast even on large codebases.

### Graceful Degradation

The code index works with SQLite alone and adds capabilities as optional backends become available:

| Backend | Capability | Dependency |
|---------|-----------|------------|
| **SQLite** | Name search, symbol storage, file outlines | None (always available) |
| **Qdrant** | Semantic search (find by description) | Qdrant instance |
| **Neo4j** | Call graph, import graph, usage tracking | Neo4j instance |
| **Claude Haiku** | One-sentence symbol summaries | LLM API key |

## Tool Reference

The `gobby-code` MCP server provides 12 tools across 4 sub-registries.

### Indexing

#### `list_indexed()`

List all indexed projects with stats.

**Returns:** `list[dict]` with `project_id`, `root_path`, `total_files`, `total_symbols`, `last_indexed_at`, `index_duration_ms`.

#### `invalidate_index(project_id?)`

Clear the index for a project, forcing a full re-index on the next run.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `project_id` | string | No | Defaults to current project |

### Query

#### `get_file_tree(project_id?)`

File tree with symbol counts per file.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `project_id` | string | No | Defaults to current project |

**Returns:** `list[dict]` with `file_path`, `language`, `symbol_count`, `byte_size`.

#### `get_file_outline(file_path, project_id?)`

Hierarchical symbol outline for a single file. Much cheaper than reading the full file.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `file_path` | string | Yes | Path to the file |
| `project_id` | string | No | Defaults to current project |

**Returns:** `dict` with `file_path`, `symbol_count`, and `symbols` array. Each symbol includes `id`, `name`, `qualified_name`, `kind`, `line_start`, `line_end`, `signature`, `docstring` (first 200 chars), `summary`, and `parent_id` (if nested).

#### `get_symbol(symbol_id, project_id?)`

Get full source code for a symbol by ID. O(1) retrieval via byte offsets.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `symbol_id` | string | Yes | Symbol UUID |
| `project_id` | string | No | Defaults to current project |

**Returns:** All symbol fields plus `source` (extracted from the file using byte offsets).

#### `get_symbols(symbol_ids, project_id?)`

Batch-retrieve multiple symbols by ID.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `symbol_ids` | list[string] | Yes | List of symbol UUIDs |
| `project_id` | string | No | Defaults to current project |

#### `search_symbols(query, project_id?, kind?, file_path?, limit?)`

Hybrid search combining name matching, semantic similarity, and graph ranking via Reciprocal Rank Fusion.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `query` | string | Yes | Search query (name or description) |
| `project_id` | string | No | Defaults to current project |
| `kind` | string | No | Filter by symbol kind (function, class, method, etc.) |
| `file_path` | string | No | Filter to a specific file |
| `limit` | integer | No | Max results (default: 20) |

**Returns:** `dict` with `results` (each including `_score` and `_sources`), `status` ("current" or "stale"), and optionally `stale_files` if the index is outdated.

#### `search_text(query, project_id?, file_path?, limit?)`

Full-text search across symbol names and signatures. SQLite-only, no semantic component.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `query` | string | Yes | Substring to search for |
| `project_id` | string | No | Defaults to current project |
| `file_path` | string | No | Filter to a specific file |
| `limit` | integer | No | Max results (default: 20) |

### Graph

These tools require Neo4j. They return an error dict if Neo4j is unavailable.

#### `find_callers(symbol_name, project_id?, limit?)`

Find symbols that call a given function or method.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `symbol_name` | string | Yes | Name of the function/method |
| `project_id` | string | No | Defaults to current project |
| `limit` | integer | No | Max results (default: 20) |

**Returns:** `list[dict]` with `caller_id`, `caller_name`, `file`, `line`.

#### `find_usages(symbol_name, project_id?, limit?)`

Find all usages of a symbol (calls + imports).

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `symbol_name` | string | Yes | Name of the symbol |
| `project_id` | string | No | Defaults to current project |
| `limit` | integer | No | Max results (default: 20) |

**Returns:** `list[dict]` with `source_id`, `source_name`, `rel_type`, `file`, `line`.

#### `get_imports(file_path, project_id?)`

Get the import graph for a file.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `file_path` | string | Yes | Path to the file |
| `project_id` | string | No | Defaults to current project |

**Returns:** `list[dict]` with `module_name`.

### Summary

#### `get_summary(symbol_id)`

Get an AI-generated one-sentence summary for a symbol. Cached after first generation.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `symbol_id` | string | Yes | Symbol UUID |

**Returns:** `dict` with `symbol_id`, `name`, `summary`, `cached` (bool).

#### `get_repo_outline(project_id?)`

High-level project summary showing top-level directories and their symbol counts.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `project_id` | string | No | Defaults to current project |

**Returns:** `dict` with `project_id`, `root_path`, `total_files`, `total_symbols`, `last_indexed_at`, and `directories` (sorted by symbol count descending).

## Search

### Hybrid Ranking (RRF)

`search_symbols` combines up to three ranked lists using Reciprocal Rank Fusion with K=60:

```
score = 1 / (60 + rank)
```

| Source | Backend | Always Available |
|--------|---------|-----------------|
| **Name search** | SQLite `LIKE` on `name` + `qualified_name` | Yes |
| **Semantic search** | Qdrant vector similarity | Only with Qdrant |
| **Graph boost** | Neo4j callers + usages | Only with Neo4j |

Each result includes:
- `_score`: Combined RRF score (rounded to 4 decimals)
- `_sources`: List of sources that contributed (e.g., `["name", "semantic", "graph"]`)

## Languages

13 languages supported via tree-sitter:

| Language | Extensions |
|----------|-----------|
| Python | `.py`, `.pyi` |
| JavaScript | `.js`, `.jsx`, `.mjs`, `.cjs` |
| TypeScript | `.ts`, `.tsx` |
| Go | `.go` |
| Rust | `.rs` |
| Java | `.java` |
| PHP | `.php` |
| Dart | `.dart` |
| C# | `.cs` |
| C | `.c`, `.h` |
| C++ | `.cpp`, `.cc`, `.cxx`, `.hpp`, `.hxx`, `.hh` |
| Elixir | `.ex`, `.exs` |
| Ruby | `.rb`, `.rake`, `.gemspec` |

### Symbol Kinds Extracted

Functions, classes, methods, constants, types, imports, interfaces, enums, structs, traits, modules. The exact kinds depend on the language.

## Security

The parser enforces multiple security checks before indexing any file:

1. **Path traversal** — Resolved path must be within the project root
2. **Symlink safety** — Symlink targets must resolve within the project root
3. **Exclusion patterns** — Files matching configured patterns are skipped (e.g., `node_modules`, `.git`)
4. **Secret detection** — Files with sensitive extensions (`.env`, `.pem`, `.key`, `.p12`, `.secret`) or names (`credentials`, `id_rsa`, `api_key`) are skipped
5. **Size limit** — Files exceeding `max_file_size_bytes` (default 1MB) are skipped
6. **Binary detection** — Files with null bytes in the first 8KB are skipped

## Configuration

All fields in `CodeIndexConfig`:

```yaml
code_index:
  enabled: true                           # Enable code indexing
  auto_index_on_session_start: true       # Index when a session starts
  auto_index_on_commit: true              # Re-index changed files on git commit
  maintenance_interval_seconds: 300       # Background re-index interval (5 min)
  max_file_size_bytes: 1000000            # Skip files larger than 1MB
  exclude_patterns:                       # Glob patterns to skip
    - node_modules
    - .git
    - __pycache__
    - vendor
    - build
    - dist
    - .venv
  embedding_enabled: true                 # Enable Qdrant semantic vectors
  graph_enabled: true                     # Enable Neo4j call/import graph
  summary_enabled: true                   # Enable AI-generated summaries
  summary_provider: claude                # LLM provider for summaries
  summary_model: haiku                    # Model (fast/cheap)
  summary_batch_size: 50                  # Symbols per summary batch
  qdrant_collection_prefix: code_symbols_ # Vector collection name prefix
  languages:                              # Languages to index (all 13 by default)
    - python
    - javascript
    - typescript
    # ... etc.
```

## Rule Templates

Two rule templates are bundled in `src/gobby/install/shared/rules/code-index/`. Both are disabled by default and must be installed and enabled via the rules engine.

### `compress-large-reads`

Replaces large Read outputs with symbol outlines automatically.

- **Event:** `after_tool` (Read)
- **Trigger:** Output > 20,000 characters and `code_index_available` is true
- **Effect:** `compress_output` with `compressor: code_index`
- The compressed output includes the first 50 lines of the file plus a symbol outline table with IDs for targeted retrieval via `get_symbol()`

### `nudge-on-large-read`

Injects a context hint after reading a large indexed file, suggesting `gobby-code` tools instead.

- **Event:** `after_tool` (Read)
- **Trigger:** Output > 10,000 characters and `code_index_available` is true
- **Effect:** `inject_context` with tool suggestions (`get_file_outline`, `search_symbols`, `get_symbol`)

## Auto-Indexing

The code index runs automatically in three scenarios:

### Session Start

When a session begins, `POST /api/code-index/session-start` triggers a full incremental index. If files are indexed, the session variable `code_index_available` is set to `true`, enabling rule templates.

### Git Commit

After a git commit, `POST /api/code-index/incremental` re-indexes only the changed files. The request body includes the list of changed file paths.

### Background Maintenance

A background loop runs every `maintenance_interval_seconds` (default: 300s / 5 min), re-indexing any files whose content hash has changed since the last pass.

## HTTP Endpoints

### `POST /api/code-index/incremental`

Index specific changed files (called by git hooks).

**Request:**
```json
{
  "files": ["src/app.py", "src/utils.py"],
  "project_id": ""
}
```

**Response:**
```json
{
  "files_indexed": 2,
  "symbols_found": 15,
  "files_skipped": 0,
  "duration_ms": 120
}
```

### `POST /api/code-index/session-start`

Full incremental index on session start.

**Request:**
```json
{
  "project_id": "abc-123",
  "root_path": "/path/to/project",
  "session_id": ""
}
```

### `GET /api/code-index/status`

Check indexing status.

**Query parameter:** `project_id` (optional). Without it, returns all indexed projects.

**Response:**
```json
{
  "root_path": "/path/to/project",
  "total_files": 142,
  "total_symbols": 3580,
  "last_indexed_at": "2026-03-06T10:00:00Z",
  "index_duration_ms": 4200
}
```

## Typical Workflow

1. Session starts → auto-index runs → `code_index_available = true`
2. Agent needs to understand a file → `get_file_outline("src/app.py")` instead of reading it
3. Agent finds a symbol of interest → `get_symbol("a1b2c3d4-...")` for just that function's source
4. Agent needs to find related code → `search_symbols("authentication handler")`
5. Agent traces callers → `find_callers("validate_token")`

This workflow reads only the symbols needed, saving 90%+ tokens compared to reading entire files.

## See Also

- [tool-compression.md](tool-compression.md) — Output and code index compression
- [mcp-tools.md](mcp-tools.md) — Complete MCP tool reference
- [search.md](search.md) — Unified search with TF-IDF and embeddings
- [rules.md](rules.md) — Rule engine reference
- [configuration.md](configuration.md) — Full configuration reference

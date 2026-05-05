# Code Index

Gobby's code index is a native `gcode` CLI plus daemon-side storage and graph
services. Use it to search symbols, inspect outlines, retrieve exact symbol
source, and trace graph relationships without reading whole source files.

The current authoring surface is `gcode`. Older Gobby CLI and MCP examples for
code-index access are stale and should be replaced with the commands below.

## Quick Start

Index the current project, check status, and force a rebuild:

```bash
gcode index
gcode status
gcode index --full
gcode invalidate --force
```

Use indexed navigation before opening large files:

```bash
gcode search "task validation"
gcode search-symbol "TaskValidator" --kind class
gcode search-content "code_index_available" --path "src/**/*.py"
gcode outline src/gobby/tasks/validation.py
gcode symbol <symbol-id>
```

Graph commands require the Gobby daemon and graph backend:

```bash
gcode callers validate_task
gcode imports src/gobby/tasks/validation.py
gcode blast-radius validate_task --depth 3
gcode graph rebuild
```

If `gcode` is missing, run `gobby install`. Gobby's daemon-side incremental
trigger logs a warning and skips code indexing when the native binary is not
installed.

## How It Works

```mermaid
flowchart TB
    A[Source tree] --> B[gcode index]
    B --> C[SQLite symbols, files, chunks]
    C --> D[gcode search and outline commands]
    C --> E[Daemon sync worker]
    E --> F[Qdrant vectors]
    E --> G[Neo4j graph]
    E --> H[Symbol summaries]
    G --> I[gcode callers, imports, blast-radius]
    G --> J[/api/code-index/graph routes]
```

`gcode index` owns parsing and writes symbols, indexed files, content chunks,
imports, and call relationships to SQLite. The daemon owns integrations around
that store: background maintenance, optional vector and graph sync, optional
symbol summaries, HTTP graph routes, and session variables.

Files are indexed incrementally by content hash. Changed files are re-parsed;
unchanged files are skipped. The post-edit trigger batches file notifications
and runs:

```bash
gcode index --files <changed-files> --quiet
```

The maintenance loop runs every `code_index.maintenance_interval_seconds`
seconds and checks indexed projects for stale files. When enabled, a sync worker
copies indexed data to Qdrant and Neo4j, and a summarizer fills missing symbol
summaries.

## CLI Reference

All commands accept these global options unless noted:

| Option | Description |
| :--- | :--- |
| `--project <PROJECT>` | Override project root detection |
| `--format json\|text` | Select JSON or text output; JSON is the default |
| `--quiet` | Suppress warnings |
| `--verbose` | Enable verbose output |
| `--no-freshness` | Skip read-time freshness checks |

### Index Lifecycle

| Command | Purpose |
| :--- | :--- |
| `gcode init` | Initialize `.gobby/gcode.json` project context |
| `gcode index [PATH]` | Index a directory; defaults to the project root |
| `gcode index --files <FILES>...` | Index only specific files |
| `gcode index --full` | Force a full re-index |
| `gcode status` | Show indexed file, symbol, and timing stats |
| `gcode invalidate --force` | Clear index data so the next index is fresh |
| `gcode projects` | List indexed projects |
| `gcode prune` | Remove stale project entries |

### Search And Retrieval

| Command | Purpose |
| :--- | :--- |
| `gcode search <QUERY>` | Hybrid search: FTS5 plus optional semantic and graph boost |
| `gcode search-symbol <QUERY>` | Exact-first symbol/name lookup |
| `gcode search-text <QUERY>` | FTS5 search over symbol names, signatures, and docstrings |
| `gcode search-content <QUERY>` | FTS5 search over file content chunks |
| `gcode outline <FILE>` | Hierarchical symbol outline for one file |
| `gcode symbol <ID>` | Fetch one symbol's source by byte offset |
| `gcode symbols <IDS>...` | Fetch multiple symbols by ID |
| `gcode kinds` | List indexed symbol kinds |
| `gcode tree` | File tree with symbol counts |
| `gcode repo-outline` | Directory-grouped project stats |

Search commands support `--limit`, `--offset`, `--language`, and `--path`.
Symbol searches also support `--kind`.

### Graph Queries

These commands require the Gobby daemon and graph support:

| Command | Purpose |
| :--- | :--- |
| `gcode callers <SYMBOL_NAME>` | Find callers of the symbol resolved from a query |
| `gcode usages <SYMBOL_NAME>` | Find incoming call usages for the resolved symbol |
| `gcode imports <FILE>` | Show import graph for one file |
| `gcode blast-radius <TARGET>` | Trace transitive impact from a symbol query |
| `gcode graph clear` | Clear the current project's graph projection |
| `gcode graph rebuild` | Rebuild the graph projection from SQLite |

## Indexed Data

The SQLite store tracks:

| Data | Notes |
| :--- | :--- |
| Projects | Root path, total files, total symbols, indexed timestamp, duration |
| Files | Path, language, content hash, symbol count, byte size, sync flags |
| Symbols | Name, qualified name, kind, language, byte offsets, line range, signature, docstring, summary |
| Imports | Source file to imported module |
| Calls | Caller/callee relationships, including unresolved and external targets |
| Content chunks | Searchable chunks for comments, strings, configs, docs, and other non-symbol text |

Primary storage is always SQLite. Qdrant adds semantic search and Neo4j adds
graph traversal when configured and available. Symbol summaries are cached in
SQLite and invalidated when a symbol's content hash changes.

## Languages And Content

AST symbol extraction is configured for:

| Family | Languages |
| :--- | :--- |
| Core app languages | Python, JavaScript, TypeScript, Go, Rust, Java |
| Additional runtimes | PHP, Dart, C#, C, C++, Elixir, Ruby |
| Structured docs/config | Markdown, YAML, JSON |

Additional content-only extensions are indexed for text search, including
`.html`, `.css`, `.scss`, `.less`, `.toml`, `.cfg`, `.ini`, shell scripts,
`.sql`, `.graphql`, `.proto`, `.txt`, `.rst`, `.csv`, `.gitignore`, and
`.editorconfig`.

## Configuration

Configure indexing in `code_index`:

```yaml
code_index:
  enabled: true
  auto_index_on_commit: true
  maintenance_interval_seconds: 300
  max_file_size_bytes: 1000000
  exclude_patterns:
    - node_modules
    - .vite
    - .git
    - __pycache__
    - .mypy_cache
    - .ruff_cache
    - .pytest_cache
    - vendor
    - build
    - dist
    - .venv
  embedding_enabled: true
  graph_enabled: true
  qdrant_collection_prefix: code_symbols_
  summary_enabled: true
  summary_provider: claude
  summary_model: haiku
  summary_batch_size: 20
  sync_worker_interval_seconds: 5.0
  sync_worker_batch_size: 50
  languages:
    - python
    - javascript
    - typescript
    - markdown
    - yaml
    - json
```

## Daemon Integration

### Session Start

On session start, Gobby checks existing index stats. If the project has indexed
symbols, the session variable `code_index_available` is set to `true`, allowing
rules to teach or enforce indexed navigation.

### Post-Edit Incremental Indexing

`CodeIndexTrigger` receives file-change notifications, debounces them by root
path, normalizes file paths under the project root, and runs `gcode index
--files ... --quiet` for the changed files.

### Background Maintenance

The maintenance loop checks indexed projects for stale files on the configured
interval. The sync worker can then update Qdrant vectors, Neo4j graph edges, and
missing summaries in batches.

## HTTP Endpoints

The daemon exposes graph and invalidation routes under `/api/code-index`:

| Method | Route | Purpose |
| :--- | :--- | :--- |
| `GET` | `/api/code-index/graph` | File-level graph overview; query `project_id`, `limit` |
| `GET` | `/api/code-index/graph/file/{file_path}` | Symbols and graph context for one file; query `project_id` |
| `GET` | `/api/code-index/graph/symbol/{symbol_id}/neighbors` | Symbol neighbors; query `project_id`, `limit` |
| `GET` | `/api/code-index/graph/blast-radius` | Impact graph; query `project_id` and exactly one of `symbol_id` or `file_path`, plus `depth`, `limit` |
| `GET` | `/api/code-index/graph/search` | Symbol search for graph UI; query `project_id`, `q`, `limit` |
| `POST` | `/api/code-index/graph/clear` | Clear one project's graph projection; query `project_id` |
| `POST` | `/api/code-index/graph/rebuild` | Rebuild one project's graph projection; query `project_id`, `limit` |
| `POST` | `/api/code-index/invalidate` | Clear all index data for a project; JSON body `{"project_id": "..."}` |

Graph routes return `503` when the code indexer or graph backend is unavailable.
Blast-radius requests return `400` unless exactly one target is provided.

## Rules

Gobby includes a `block-and-teach-code-index` rule in the shared code-index
ruleset. When active, it blocks first-pass source reads and text searches until
the agent loads the `code-index` skill. The injected guidance points agents to:

```bash
gcode outline path/to/file
gcode search "query"
gcode symbol <id>
```

Rules are runtime state, not just template files. Check installed rule state in
the rules engine before claiming a rule is disabled.

## Typical Workflow

1. Run `gcode status` to confirm an index exists.
2. Use `gcode search`, `gcode search-symbol`, or `gcode search-content` to find
   the relevant code.
3. Use `gcode outline <FILE>` before opening a large file.
4. Use `gcode symbol <ID>` for the exact implementation when the outline points
   to a specific function, class, or method.
5. Use `gcode callers`, `gcode imports`, or `gcode blast-radius` when the change
   could affect other files.

## See Also

- [search.md](search.md) - Unified search with TF-IDF and embeddings
- [rules.md](rules.md) - Rule engine reference
- [configuration.md](configuration.md) - Full configuration reference
- [http-endpoints.md](http-endpoints.md) - HTTP API reference

_Last verified: 2026-05-04_

# Code Index

Gobby's code index is a native `gcode` CLI plus daemon-side HTTP and storage
surfaces. Use it to search symbols, inspect outlines, retrieve exact symbol
source, and trace graph relationships without reading whole source files.

The current user-facing surface is `gcode`. Older Gobby CLI and MCP examples
for direct code-index access are stale; use the commands below.

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
gcode grep "spawn_ui_server(" src -m 50
gcode search "task validation"
gcode search-symbol "TaskValidator" --kind class
gcode search-content "code_index_available" "src/**/*.py"
gcode outline src/gobby/tasks/validation.py
gcode symbol <symbol-id>
```

Graph commands require the Gobby daemon. `callers`, `usages`, `imports`, and
`blast-radius` read graph data through top-level commands. `gcode graph` owns
projection sync, lifecycle commands, and the read API used by the daemon shim:

```bash
gcode callers validate_task
gcode imports src/gobby/tasks/validation.py
gcode blast-radius validate_task --depth 3
gcode graph sync-file --file src/gobby/runner.py
gcode graph rebuild
```

If `gcode` is missing, run `gobby install`. Gobby's daemon-side incremental
trigger logs a warning and skips code indexing when the native binary is not
installed.

## How It Works

```mermaid
flowchart TB
    A[Source tree] --> B[gcode index]
    B --> C[PostgreSQL hub symbols, files, chunks]
    C --> D[gcode search and outline commands]
    C --> E[Daemon sync worker]
    E --> F[Qdrant vectors]
    E --> G[FalkorDB graph]
    E --> H[Symbol summaries]
    G --> I[gcode callers, imports, blast-radius]
    G --> J[gcode graph read commands]
    J --> K[/api/code-index/graph HTTP shim]
```

`gcode index` owns parsing and writes symbols, indexed files, content chunks,
imports, and call relationships through the PostgreSQL hub. `gcode graph` owns
the code graph projection in FalkorDB, including `sync-file`, `clear`,
`rebuild`, and graph read operations. The daemon owns integrations around those
rows: background maintenance, optional vector sync, symbol autocomplete, HTTP
graph shim routes, optional symbol summaries, and session variables.

Files are indexed incrementally by content hash. Changed files are re-parsed;
unchanged files are skipped. The post-edit trigger ignores `.gobby/` internal
edits, batches repo-relative file notifications by project root with a
two-second debounce, and runs:

```bash
gcode index --files <changed-files> --quiet
```

The maintenance loop runs every `code_index.maintenance_interval_seconds`
seconds. It replays `gcode index --project <root> --quiet` for each indexed
project, purges projects whose root no longer exists, and fills missing symbol
summaries when a summarizer is configured. A separate sync worker polls pending
files, copies symbols to Qdrant vectors, and calls `gcode graph sync-file --file
<file> --project <root>` for graph projection sync.

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
| `gcode grep <PATTERN> [PATH...]` | Exact indexed content grep over file content chunks |
| `gcode search <QUERY>` | Hybrid search: full-text plus optional semantic and graph boost |
| `gcode search-symbol <QUERY>` | Exact-first symbol/name lookup |
| `gcode search-text <QUERY>` | Full-text search over symbol names, signatures, and docstrings |
| `gcode search-content <QUERY>` | Full-text search over file content chunks |
| `gcode outline <FILE>` | Hierarchical symbol outline for one file |
| `gcode symbol <ID>` | Fetch one symbol's source by byte offset |
| `gcode symbols <IDS>...` | Fetch multiple symbols by ID |
| `gcode kinds` | List indexed symbol kinds |
| `gcode tree` | File tree with symbol counts |
| `gcode repo-outline` | Directory-grouped project stats |

Ranked search commands support `--limit`, `--offset`, `--language`, and
positional path filters after the query. `gcode search` and
`gcode search-symbol` also support `--kind`. `gcode grep` supports positional
paths, `-g/--glob`, `-i`, `-F`, `-C/-A/-B`, and `-m/--max-count`.

### Graph Queries

These commands require the Gobby daemon and graph support:

| Command | Purpose |
| :--- | :--- |
| `gcode callers <SYMBOL_NAME>` | Find callers of the symbol resolved from a query |
| `gcode usages <SYMBOL_NAME>` | Find incoming call usages for the resolved symbol |
| `gcode imports <FILE>` | Show import graph for one file |
| `gcode blast-radius <TARGET>` | Trace transitive impact from a symbol query |
| `gcode graph sync-file --file <FILE>` | Sync one indexed file into the graph projection |
| `gcode graph clear` | Clear the current project's graph projection |
| `gcode graph clear --project-id <ID>` | Clear a graph projection without resolving a project root |
| `gcode graph rebuild` | Rebuild the graph projection from indexed hub rows |

`gcode callers` and `gcode usages` support `--limit` and `--offset`. `gcode
blast-radius` supports `--depth` and `--limit`.

## Indexed Data

The PostgreSQL hub-backed code-index store tracks:

| Data | Notes |
| :--- | :--- |
| Projects | Root path, total files, total symbols, indexed timestamp, duration |
| Files | Path, language, content hash, symbol count, byte size, sync flags |
| Symbols | Name, qualified name, kind, language, byte offsets, line range, signature, docstring, summary |
| Imports | Source file to imported module |
| Calls | Caller/callee relationships, including unresolved and external targets |
| Content chunks | Searchable chunks for comments, strings, configs, docs, and other non-symbol text |

The code-index tables live in the runtime PostgreSQL hub; Qdrant adds semantic
search and FalkorDB adds graph traversal when configured and available. Symbol
summaries are cached in the code-index rows and invalidated when a symbol's
content hash changes.

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
    - .tox
    - .eggs
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
    - go
    - rust
    - java
    - php
    - dart
    - csharp
    - c
    - cpp
    - elixir
    - ruby
    - markdown
    - yaml
    - json
  content_extensions:
    - .html
    - .css
    - .scss
    - .less
    - .toml
    - .cfg
    - .ini
    - .sh
    - .bash
    - .zsh
    - .fish
    - .sql
    - .graphql
    - .proto
    - .txt
    - .rst
    - .csv
    - .gitignore
    - .editorconfig
```

## Daemon Integration

### Session Start

On session start, Gobby checks existing index stats. If the project has indexed
symbols, the session variable `code_index_available` is set to `true`. Rules can
then teach or enforce indexed navigation for that session.

### Post-Edit Incremental Indexing

`CodeIndexTrigger` receives file-change notifications from post-tool hook
handling, debounces them by root path, normalizes paths under the project root,
and runs `gcode index --files ... --quiet` for the changed files from the root
as the subprocess working directory. If `gcode` is not installed, the trigger
logs a warning and skips the incremental update.

### Background Maintenance

The maintenance loop checks indexed projects on the configured interval and
uses `gcode index --project <root> --quiet` for refresh. The sync worker can
then update Qdrant vectors and call `gcode graph sync-file --file <file>
--project <root>` for graph projection sync. Summary generation runs from
maintenance when `code_index.summary_enabled` is true and the daemon has an LLM
service.

## HTTP Endpoints

The daemon exposes graph and invalidation routes under `/api/code-index`:

| Method | Route | Purpose |
| :--- | :--- | :--- |
| `GET` | `/api/code-index/graph` | File-level graph overview; query `project_id`, `limit` |
| `GET` | `/api/code-index/graph/file/{file_path}` | Symbols and graph context for one file; query `project_id` |
| `GET` | `/api/code-index/graph/symbol/{symbol_id}/neighbors` | Symbol neighbors; query `project_id`, `limit` |
| `GET` | `/api/code-index/graph/blast-radius` | Impact graph; query `project_id` and exactly one of `symbol_id` or `file_path`, plus `depth`, `limit` |
| `GET` | `/api/code-index/graph/search` | Symbol search for graph UI; query `project_id`, `q`, `limit` |
| `POST` | `/api/code-index/graph/clear` | Clear one project's graph projection through `gcode graph clear --project-id`; query `project_id` |
| `POST` | `/api/code-index/graph/rebuild` | Rebuild one project's graph projection through `gcode graph rebuild --project <root>`; query `project_id`, legacy `limit` accepted but ignored |
| `POST` | `/api/code-index/invalidate` | Clear all index data for a project; JSON body `{"project_id": "..."}` |

All graph routes require `project_id`; missing values return `400`. The graph
overview, file, symbol-neighbors, and blast-radius routes resolve the project
root from daemon storage, then call `gcode graph` with `--project <root>`.
Missing or stale `gcode` returns `503`. Graph command, timeout, and JSON errors
return `500`. Graph search stays daemon-owned because the UI uses PostgreSQL
symbol autocomplete. Blast-radius requests return `400` unless exactly one of
`symbol_id` or `file_path` is provided. Invalidation returns `{"status": "ok",
"note": "not indexed"}` when the project has no index record.

## Rules

Gobby includes a `require-code-index-skill` rule in the shared code-index
ruleset. When active, it blocks first-pass code navigation reads and searches
until the agent loads the `code-index` skill. The loaded guidance points agents
to:

```bash
gcode grep "pattern" [PATH...] -m 50
gcode search-content "query" [PATH...]
gcode search-symbol "name" [PATH...]
gcode outline path/to/file
gcode symbol <id>
gcode callers <symbol_name>
gcode usages <symbol_name>
```

Rules are runtime state, not just template files. Check installed rule state in
the rules engine before claiming a rule is disabled.

## Typical Workflow

1. Run `gcode status` to confirm an index exists.
2. Use `gcode grep` for exact strings and call sites, `gcode search-content`
   for ranked text, `gcode search-symbol` for known names, or `gcode search`
   for fuzzy concepts.
3. Use `gcode outline <FILE>` before opening a large file.
4. Use `gcode symbol <ID>` for the exact implementation when the outline points
   to a specific function, class, or method.
5. Use `gcode callers`, `gcode usages`, `gcode imports`, or
   `gcode blast-radius` when the change could affect other files.

## See Also

- [search.md](search.md) - Unified search with TF-IDF and embeddings
- [rules.md](rules.md) - Rule engine reference
- [configuration.md](configuration.md) - Full configuration reference
- [http-endpoints.md](http-endpoints.md) - HTTP API reference

_Last verified: 2026-06-02_

# gcode User Guide

A complete guide to using `gcode` for code search, symbol navigation, and dependency analysis.

## Getting Started

### Install

Download the `gcode-v*` release from [GitHub Releases](https://github.com/GobbyAI/gobby/releases) or build from source:

```bash
cargo install gobby-code
```

Graph and semantic features are configured at runtime, not behind Cargo feature
flags. Gobby-managed projects provide the required Docker-backed stack:
PostgreSQL with `pg_search`, FalkorDB, Qdrant, and an embedding endpoint.

Runtime indexing/search requires Gobby's PostgreSQL hub. gcode acquires a
signed grant from the local daemon and uses the grant DSN. Start the Gobby
daemon and apply hub schema with `gdaemon apply`. Client DSN environment
variables and local credential files are not used.

If you use [Gobby](https://github.com/GobbyAI/gobby), gcode is already installed.

### Initialize and Index

```bash
cd your-project
gcode init
```

`gcode init` does everything in one step:
1. Creates `.gobby/gcode.json` (project identity file)
2. Installs AI CLI skills for supported project-local targets
3. Indexes the entire project with tree-sitter AST parsing plus non-binary text files

You'll see a progress bar while indexing:

```text
[████████████░░░░░░░░] 18/32 : src/config.rs
```

After init, you can search immediately.

For non-Gobby-managed projects, `gcode init` installs the bundled `gcode` skill
for Claude Code, Codex, Droid, Grok, Qwen, and AGY:

| CLI | Project-local files |
|-----|---------------------|
| Claude Code | `.claude-plugin/plugin.json`, `skills/gcode/SKILL.md` |
| Codex | `.codex/skills/gcode/SKILL.md` |
| Droid | `.factory/skills/gcode/SKILL.md` |
| Grok | `.grok/skills/gcode/SKILL.md` |
| Qwen | `.qwen/skills/gcode/SKILL.md` |
| AGY | `.agents/skills/gcode/SKILL.md` |

Gobby-managed projects skip these project-local writes because Gobby owns CLI
wiring.

### First Search

```bash
gcode search "handleAuth"
```

Returns matching symbols ranked by relevance — function names, class definitions, method signatures — with file paths, line numbers, and signatures. JSON output is wrapped in a pagination envelope showing `total`, `offset`, and `limit`.

## Search

gcode offers four search modes for different use cases.

### Hybrid Search (`gcode search`)

The default. Combines pg_search BM25 text matching with semantic similarity,
graph boost, and graph expansion using Reciprocal Rank Fusion. Full hybrid
ranking requires PostgreSQL with `pg_search`, Qdrant, FalkorDB, and a reachable
embedding endpoint; the daemon grant and hub schema provide that stack. When
semantic or graph services are unavailable, search degrades to the reachable
sources instead of failing as long as BM25 search is available. JSON results
still expose `score` as the final display rank score, `rrf_score` as the raw RRF
contribution, and sorted `sources` values for source attribution.

```bash
gcode search "database connection pool"
gcode search "auth" --limit 5
gcode search "handler" --kind function
gcode search "config" --offset 10              # Page 2 of results
gcode search "Memory" src/storage              # Scope to directory
gcode search "Memory" src/storage tests/**/*.rs
gcode search "Context" --language rust         # Scope to Rust sources
```

**When to use:** General-purpose queries. Best for natural language and conceptual searches.

**Options:**
- `--limit N` — Max results (default: 10)
- `--offset N` — Skip first N results for pagination (default: 0)
- `--kind <kind>` — Filter by symbol kind: `function`, `class`, `method`, `type`, etc. Use `gcode kinds` to list what's available in the current index.
- `--language <lang>` — Filter by source language (e.g. `rust`, `python`, `typescript`, `css`).
- `--token-budget N` — Page complete hits under an approximate token ceiling. The estimate is `ceil(chars/4)` for the fully rendered page; JSON returns `next_offset`, while text prints the exact continuation command.
- Positional `PATH` arguments after the query — Filter by one or more paths or globs (e.g. `src`, `src/**/*.rs`, `tests/*`). Bare paths match the exact file path and descendants; multiple paths use OR semantics.

`--kind`, `--language`, and positional paths compose — combine them to narrow as far as you need. Globs that cannot be converted to SQL prefixes are still honored through post-filtering; JSON output includes a hint and text output prints a warning when that broader fetch path is used.

### Symbol Search (`gcode search-symbol`)

Exact-first symbol/name lookup with deterministic ranking. Resolves precise
matches (exact name, then qualified-name and case-insensitive variants) before
falling back to BM25. Useful when you already know (most of) the name and want
the canonical hit at rank 0 instead of letting hybrid ranking rerank it.

```bash
gcode search-symbol "outline"
gcode search-symbol "Context" --kind class --language rust
gcode search-symbol "ensure_fresh" crates/gcode
gcode search-symbol "Context" crates/gcode/src --kind class --language rust
gcode search-symbol "Context" --with-graph
```

**When to use:** You know the symbol's name (or close to it) and want a stable, top-ranked match — for example, before calling `gcode symbol <id>`.

**Options:** `--limit N`, `--offset N`, `--token-budget N`, `--kind <kind>`, `--language <lang>`, `--with-graph`, positional `PATH ...`. `--with-graph` keeps exact-first ranking but adds FalkorDB graph neighbors when available.

### Text Search (`gcode search-text`)

pg_search BM25 search on symbol metadata: names, qualified names, signatures, and docstrings.

```bash
gcode search-text "parseConfig"
gcode search-text "parseConfig" src
gcode search-text "parseConfig" src/**/*.py tests
gcode search-text "parseConfig" --language python
```

**When to use:** You know the exact name or part of a symbol name. Fastest mode.

**Options:** `--limit N`, `--offset N`, `--token-budget N`, `--language <lang>`, positional `PATH ...`

### Indexed Grep (`gcode grep`)

Exact indexed search over the same `code_content_chunks` corpus used by
`search-content`. It scans indexed chunks after path and glob filters, returns
stable `file_path` then line-number ordering, and never shells out to `rg`.

```bash
gcode grep "pattern" [PATH ...]
gcode grep "pattern" src -m 50
gcode grep "GOBBY_FALKORDB_HOST" -F -g "*.rs" crates/gcode/src
gcode grep "todo" --ignore-case -C 2 docs
```

**When to use:** You need grep-shaped exact matches with line numbers and
optional context. Text output uses `path:line:match` and `path-line-context`.
JSON output includes `project_id`, `pattern`, flags, `max_count`,
`matched_lines`, `truncated`, `scanned_chunks`, and per-line matches with
spans and context.

**Options:** `-n/--line-number` (accepted; text always shows line numbers),
`-i/--ignore-case`, `-F/--fixed-strings`, `-C/--context N`,
`-A/--after-context N`, `-B/--before-context N`, `-g/--glob GLOB`,
`-m/--limit N`, `--offset N`, `--token-budget N`, positional `PATH ...`.
`--max-count` is an alias for `--limit`. Use raw `rg` for filesystem grep
or unsupported ripgrep flags.

For `-g/--glob`, a bare glob such as `*.rs` matches basenames in any directory,
while a glob containing `/`, such as `src/*.rs`, matches the indexed path.

### Content Search (`gcode search-content`)

pg_search BM25 search across file content chunks. It covers AST-supported
source bodies and comments plus safe repo text files such as docs, Markdown,
skill files, configs (YAML/TOML/JSON/etc.), SQL/CSS, scripts,
`Dockerfile`/`Makefile`, and extensionless text.

```bash
gcode search-content "TODO: refactor"
gcode search-content "GOBBY_FALKORDB_HOST" *.py
gcode search-content "database_url" crates/gcode/src docs/**/*.md
gcode search-content "primary-color" --language css
```

**When to use:** Searching for string literals, comments, configuration values, stylesheet rules, or patterns that aren't symbol names.

Unsupported text files use their extension as the language label when one
exists, otherwise `text`. Binary, secret-like, excluded, empty, and >10MB files
are skipped.

**Options:** `--limit N`, `--offset N`, `--token-budget N`, `--language <lang>`, positional `PATH ...`

## Symbol Retrieval

### Outline

Get the hierarchical symbol tree for a file:

```bash
gcode outline src/config.rs
```

Returns functions, classes, methods, structs, Markdown headings, JSON/YAML
properties, etc. with their line ranges and signatures. Each page unit is one
complete top-level symbol subtree. Compact text omits IDs; use `--verbose` or
`--format json` when IDs are required. Much cheaper than reading the entire file.

### Symbol by ID

Fetch the exact source code of a symbol by its ID (from search or outline results):

```bash
gcode symbol "80abc77f-bdfe-5037-94a8-1ebcb753761d"
```

Returns the symbol with its full source code extracted via byte-offset read. Precise and minimal.

### Symbol by Location

Fetch the visible symbol containing a known file location:

```bash
gcode symbol-at src/auth.ts:42
gcode symbol-at src/auth.ts:42:7
gcode symbol-at src/auth.ts 42
```

Columns are 1-based byte columns. If no symbol contains the location, `symbol-at`
returns the nearest visible symbol and marks the JSON `lookup.match_kind` as
`nearest`; text output prints only the selected source and emits a concise stderr
fallback diagnostic unless `--quiet` is set.

### Batch Retrieve

Fetch multiple symbols in one call:

```bash
gcode symbols "id1" "id2" "id3"
```

### Symbol Kinds

List all distinct symbol kinds in the current project index:

```bash
gcode kinds
```

Returns kinds like `function`, `class`, `method`, `type`, `struct`, etc. Useful for understanding what `--kind` values are available for search filtering.

### File Tree

Get the project's file tree with symbol counts per file:

```bash
gcode tree
gcode tree crates/gcode/src 'tests/**/*.rs'
```

Useful for understanding project structure at a glance or scoping it to files,
directory prefixes, and globs. Multiple paths use OR semantics. Content-only
text files appear with a zero symbol count once indexed.

## Code Documentation

CodeWiki generation is owned by `gwiki code`. The command remains available for
isolated/manual use, while production-vault execution is operationally paused
pending the wiki redesign:

```bash
gwiki --project . code --out /tmp/codewiki-check
gwiki --project . code --scope crates/gcode/src --out /tmp/codewiki-check
```

See the [CodeWiki guide](./codewiki.md) for the paused daemon contract, manual
CLI modes, and output layout.

## Dependency Graph

Read-side graph commands require FalkorDB. Gobby-managed projects provide this
through the Docker-backed stack, and daemon-independent projects configure it
with `GOBBY_FALKORDB_HOST`, `GOBBY_FALKORDB_PORT`, and
`GOBBY_FALKORDB_PASSWORD`. Without FalkorDB, graph read commands report the
degraded state and callers that can preserve lexical results do so.

All read-side graph commands resolve fuzzy input — you don't need the exact
symbol name. Resolution tries exact match, then substring match, then BM25
search across names, signatures, and docstrings. When graph resolution remains
ambiguous, the command fails closed and prints the candidate matches; rerun
with a more specific query, UUID, or path scope to disambiguate.

For Python, JavaScript, and TypeScript, graph edges are import-aware. Calls to
external packages/modules stay external instead of being misclassified as local
symbol-to-symbol edges.

### Graph Overview

```bash
gcode graph overview --limit 100
```

- `--limit N` caps the number of files used as overview graph roots
- Default: `100`
- Output uses the global `--format` flag; default output remains `json`

### Graph Lifecycle

`gcode` owns code-index lifecycle commands, including graph clear/rebuild. These
commands use the current resolved project context and require FalkorDB:

```bash
gcode graph clear
gcode graph clear --project-id <PROJECT_ID>
gcode graph sync-file --file <FILE>
gcode graph rebuild
```

- `gcode graph clear` clears the current project's graph projection
- `gcode graph clear --project-id <PROJECT_ID>` is for daemon stale-project cleanup and runs without cwd project-root resolution
- `gcode graph sync-file --file <FILE>` syncs one already-indexed file into the graph projection
- `gcode graph rebuild` rebuilds the current project's graph projection from PostgreSQL facts
- These commands fail if required project context cannot be resolved or if FalkorDB is unavailable
- They respect the existing global `--format` flag; default output remains `json`
- No confirmation prompt is shown; these are project-scoped graph projection operators, not full index invalidation
- Graph clears delete only code-index projection nodes and edges in FalkorDB; memory graph data is left untouched
- `gcode graph sync-file --allow-missing-indexed-file` is daemon/background-worker only. It converts a missing indexed file into a skipped JSON payload with `reason: "indexed_file_not_found"`; strict human defaults return a typed error with exit code `2`.
- `gcode graph sync-file` returns a terminal skipped payload with `reason: "no_graph_facts"` for indexed files with no imports, symbols, or calls after deleting any stale file projection and marking the file graph-synced.

### Callers

Who calls this function?

```bash
gcode callers "handleAuth"
gcode callers "handleAuth" --limit 20
gcode callers "handleAuth" --offset 10    # Page 2
```

### Usages

Incoming call sites:

```bash
gcode usages "DatabasePool"
gcode usages "DatabasePool" --token-budget 120
```

### Imports

Show the import graph for a file:

```bash
gcode imports src/auth/middleware.ts
```

### Shortest Path

Find the shortest `CALLS` path from one symbol to another:

```bash
gcode path "handleRequest" "writeToDatabase"
gcode path "handleRequest" "writeToDatabase" --max-depth 8
```

Both arguments are fuzzy symbol queries resolved the same way as the other graph
commands. `--max-depth` caps how many `CALLS` hops are searched (default: `8`).

### Blast Radius

Transitive impact analysis — what breaks if this changes?

```bash
gcode blast-radius "handleAuth" --depth 3
gcode blast-radius "handleAuth" --depth 3 --token-budget 160
```

Walks the call graph to find all downstream dependents up to `--depth` levels
deep. Graph order remains stable across pages. `--token-budget` selects complete
relationship rows under the approximate `ceil(chars/4)` fully rendered page
budget and supplies the next offset when more remain.

## Project Management

### Status

Check the current project's index stats:

```bash
gcode status
```

Returns file count, symbol count, last indexed time, and duration.

### List Projects

See all indexed projects in the PostgreSQL hub:

```bash
gcode projects
```

### Prune And Projection Cleanup

Remove stale project records from the PostgreSQL hub and reconcile graph/vector
projections against `code_indexed_files`:

```bash
gcode prune
gcode prune --force
```

Stale-project pruning is global and keeps its confirmation prompt unless
`--force` is supplied. Plain `gcode prune` re-collects the remaining indexed
projects after stale invalidation, then deletes FalkorDB and Qdrant projection
data for file paths that no longer exist in PostgreSQL for each remaining
project. `gcode --project <path-or-name> prune` keeps projection cleanup scoped
to the resolved project.

Projection-specific cleanup is available when only one store needs
reconciliation:

```bash
gcode graph cleanup-orphans
gcode vector cleanup-orphans
```

`gcode graph cleanup-orphans` scans project-scoped `CodeFile.path` and
`CodeSymbol.file_path` values, deletes graph projection data for paths missing
from PostgreSQL, and runs project orphan cleanup once. `gcode vector
cleanup-orphans` scans `code_symbols_{project_id}` Qdrant payloads filtered by
`project_id`, then deletes vector points for paths missing from PostgreSQL.
Top-level `gcode prune` reports graph and vector cleanup failures independently
for each project so an unavailable FalkorDB does not block Qdrant cleanup, and
an unavailable Qdrant does not block graph cleanup.

### Cross-Project Queries

Query a different project by name or path:

```bash
# By name (matches against project directory basename)
gcode search --project myapp "query"

# By path
gcode search --project /home/user/projects/myapp "query"
```

Name resolution looks up the `code_indexed_projects` table in the configured PostgreSQL hub.

### Re-indexing

Incremental re-index (only changed files):

```bash
gcode index
```

Full re-index (re-processes all files, cleans stale external index entries):

```bash
gcode index --full
```

Index specific files:

```bash
gcode index --files src/config.rs docs/notes.md Dockerfile
```

`gcode index` writes symbols, files, chunks, imports, and calls to the
PostgreSQL hub. It marks graph/vector sync flags dirty; `gcode index
--sync-projections` updates FalkorDB graph edges and Qdrant code-symbol vectors
from Rust. Indexing and projection sync share one per-project lock for the full
operation, so overlapping daemon refreshes or freshness checks should skip with
`SkippedBusy` and keep serving the existing index. Deleted-file cleanup removes
code graph/vector projection rows before PostgreSQL facts are deleted, including
explicit `--files <deleted-file>` and whole-project orphan cleanup.
BM25-specific modes (`search-text`, `search-content`) work as soon as the
transaction commits. Full hybrid search uses the required PostgreSQL, FalkorDB,
Qdrant, and embedding stack once graph and vector projections sync; configured
runtime outages are reported as degradations by callers that support partial
results.

Reset the current project and rebuild from scratch (destructive — prompts for confirmation):

```bash
gcode invalidate
gcode index
```

`invalidate` deletes only rows for the current project from PostgreSQL. When
the daemon grant includes graph and vector capabilities, it also cleans only
that project's FalkorDB graph nodes and `code_symbols_{project_id}` Qdrant
projection. Use `--force` to skip the confirmation prompt.

Graph projection lifecycle is separate:

```bash
gcode graph clear
gcode graph rebuild
gcode graph cleanup-orphans
```

Use those to clear or replay graph state for the current project without
performing a full destructive code-index invalidation. Code vector lifecycle is
similarly scoped to `code_symbols_{project_id}` and does not touch Gobby memory
vector collections:

```bash
gcode vector clear
gcode vector clear --project-id <PROJECT_ID> --drop-collection
gcode vector rebuild
gcode vector cleanup-orphans
```

`--drop-collection` deletes the project's whole `code_symbols_{project_id}`
collection instead of its points; the daemon uses it when it purges an indexed
project that no machine selects any more. The `--project-id` forms of
`graph clear`, `vector clear`, and `invalidate` resolve from the caller's grant
alone, so they work for projects whose checkout is already gone.

## Operating Model

gcode is a daemon client, not a standalone database tool:
- Database: grant-resolved PostgreSQL hub DSN
- Identity: `.gobby/project.json`, `.gobby/gcode.json`, isolated root, linked worktree, or generated identity from `gcode init`
- Required service configs: FalkorDB, Qdrant, and embeddings from the signed grant and daemon-served config

Graph commands and semantic search become available when the required services
are configured; unhealthy services are reported as degraded required sources.

### Isolated and worktree-derived identities

Two cases break the usual "one `.gobby/project.json` ↔ one project id" mapping. gcode handles them automatically:

- **Isolation marker** — when `.gobby/project.json` carries `parent_project_path` or `parent_project_id` fields, gcode treats the directory as its own code-index target rather than as part of the parent. The id is a deterministic UUID5 derived from the canonical filesystem path, so the directory gets its own symbol/file rows in the PostgreSQL hub and never collides with the parent's index.
- **Linked git worktrees** — runs from inside a `git worktree add` directory resolve to the worktree's own top-level (via `git rev-parse --show-toplevel` and `git worktree list --porcelain`). The code-index id is derived from the worktree path, not from any inherited `.gobby/project.json`. If an inherited id would have been used, gcode prints a warning naming the filesystem-derived id it picked instead.

Both cases are reported by `gcode init`'s status line (`isolated`, `linked-worktree`) so it's clear which identity source resolved.

## Configuration

gcode resolves graph/vector infrastructure from the signed daemon grant and
daemon-served runtime config. FalkorDB defaults to port `16379` and graph name
`gobby_code` once a host is granted. Embedding settings come from
daemon-served `ai.embeddings.*` keys, with model `nomic-embed-text` once an
embeddings API base is configured.

Indexing git-ignore behavior resolves from grant-backed `indexing.respect_gitignore`
(default `true`) and `indexing.extra_excludes`.

`extra_excludes` adds component-name glob patterns to gcode's built-in
exclusions. Full and explicit indexing apply the same combined pattern set, and
a full scan prunes facts for files that become excluded.

The database connection is the grant DSN. There is no client DSN environment
variable or local credential file.

The daemon URL (used by `invalidate` and savings reporting) is resolved by the
shared `gobby_core::daemon_url` contract:
1. `GOBBY_DAEMON_URL` environment variable (full base URL)
2. `GOBBY_PORT` environment variable → `http://127.0.0.1:{port}`
3. `~/.gobby/bootstrap.yaml` `daemon_port` + `bind_host` keys
4. Default: `http://127.0.0.1:60887`

## Output Formats

All commands support `--format`. Navigation defaults to compact text:

```bash
gcode search "query"                 # Default compact navigation text
gcode search "query" --format json   # Stable compact JSON machine surface
```

The compact-text defaults are `search`, `search-symbol`, `search-text`,
`search-content`, `grep`, `outline`, `symbol`, `symbol-at`, `symbols`, `kinds`,
`tree`, `repo-outline`, `callers`, `callees`, `usages`, `imports`, `path`, and
`blast-radius`. Nested structural graph and lifecycle commands retain complete
JSON defaults.

JSON collection output uses a pagination envelope:

```json
{
  "project_id": "3bf57fe7-...",
  "total": 47,
  "offset": 0,
  "limit": 10,
  "next_offset": 3,
  "results": [...]
}
```

`next_offset` is omitted on the final page. `budget_exceeded` is emitted only
when one complete oversized item or page metadata exceeds the requested budget.
Existing hints remain available in JSON for search and graph diagnostics.

Graph `callers` and `usages` result rows include `confidence`. AST-derived
edges are emitted as `EXTRACTED`; future inferred edges can report `INFERRED`
or `AMBIGUOUS` while preserving the same result shape.

Collection commands accept `--limit`, `--offset`, and `--token-budget`.
Compact text automatically uses a 2,000-token budget. Each page selects the
largest prefix of complete semantic items whose fully rendered output fits the
approximate `ceil(chars/4)` budget. An oversized first item is emitted complete.
Text output prints a shell-safe command that retrieves the next page:

```text
continue: gcode search query --offset 3
```

Run the printed command unchanged to traverse pages without gaps or duplicates.

### Verbose output

`--verbose` restores IDs and diagnostics in compact search and outline output:

```bash
gcode outline src/main.rs --verbose
```

Use explicit JSON when programmatic consumers need stable result fields.

Suppress warnings and progress bars with `--quiet`:

```bash
gcode index --quiet
```

### Read-time freshness

By default, search, symbol, outline, and graph read commands check that the
indexed source still matches the on-disk file before returning results. If a
file has changed, gcode incrementally re-indexes the affected file(s)
transparently and then runs your command. This is meant to keep individual
reads honest — it is **not** a substitute for `gcode index` after a bulk
checkout or branch switch.

Allow stale index data per call when you want zero freshness-check overhead:

```bash
gcode --allow-stale search "query"
gcode --allow-stale outline src/main.rs
```

Set `GCODE_FRESHNESS_INFLIGHT=1` in nested processes (or scripts that already
run their own re-index) to short-circuit the same checks. gcode also sets this
flag internally to prevent the indexer from recursing into itself.

Incremental overlay indexing (a worktree or clone layered over its parent
project's index) reconciles only the paths that can differ from the parent:
`git status` in the overlay, `git status` in the parent checkout, and
`git diff --name-only <parent HEAD> HEAD` for commit-level divergence in either
direction. A path that differs is indexed from the overlay's own tree (or
tombstoned when it only exists in the parent), so overlay reads never serve
parent rows for content that differs on disk. Set
`GCODE_GIT_STATUS_TIMEOUT_SECS` to a positive number to override the default
5-second timeout for each of those git calls; invalid or nonpositive values are
ignored with a warning. When any call fails (for example a clone whose object
store lacks the parent's HEAD), the run falls back to hashing every discovered
path. The read-time pre-gate is overlay-aware too: a project-scope read in a
worktree skips the refresh when every visible path (the overlay's own rows plus
the parent rows it inherits) is unchanged since the overlay's last index and the
parent has not been re-indexed since; a parent re-index, a worktree edit, or a
deleted or re-created inherited file trips it.

## Troubleshooting

### "No gcode project found"

You haven't initialized the project yet:

```bash
gcode init
```

Or specify a project explicitly:

```bash
gcode search --project /path/to/project "query"
```

### "Project 'foo' not found"

The project name doesn't match any indexed project. Check available projects:

```bash
gcode projects
```

### Empty search results

- Run `gcode status` to verify the project is indexed
- Try `gcode search-text` for exact name matches
- Try `gcode grep "pattern" [PATH ...]` for exact string/comment searches
- Try `gcode search-content` for ranked string/comment searches
- Run `gcode index` to pick up recently changed files

### Graph commands return empty results

If you get "No symbol matching 'X' found", the input didn't resolve to any indexed symbol. Try a different term or check what's indexed with `gcode search-text "X"`.

If results are empty but the symbol exists, this is expected when FalkorDB is not configured. In Gobby mode, check that FalkorDB is running and configured:

```bash
echo $GOBBY_FALKORDB_HOST
echo $GOBBY_FALKORDB_PORT
gcode status
```

### `gcode graph clear` / `gcode graph rebuild` fail immediately

- If you see a project-context error, initialize the project first with `gcode init` or use `--project <path>`
- If you see a FalkorDB configuration or connectivity error, confirm `GOBBY_FALKORDB_HOST` / `GOBBY_FALKORDB_PORT` or `config_store` are correct
- For stale-project cleanup where cwd has no project context, use `gcode graph clear --project-id <PROJECT_ID>`

### Slow first index

Tree-sitter parsing is fast but scales with codebase size. Subsequent runs are incremental — only changed files are re-indexed. Large `node_modules`, `target`, `.venv` directories are excluded automatically.

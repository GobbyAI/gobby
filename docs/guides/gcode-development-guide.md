# gcode Development Guide

Technical internals for developers and agents working in the gcode codebase.

## Architecture Overview

```
CLI (main.rs, clap)
  → Context::resolve (config.rs)
    → detect_project_root / resolve_database_url / resolve_services
  → Command dispatch (main.rs match)
    → commands/{search,symbols,graph,index,status,init}.rs
      → search/ pipeline (pg_search BM25 + semantic + graph sources → RRF)
      → index/ pipeline (walker → parser → chunker → hasher → indexer)
      → falkor (FalkorDB graph queries)
      → db (PostgreSQL hub connections)
      → output (JSON/text formatting)
```

### Entry Point

`main.rs` parses CLI args via clap, resolves a `config::Context` (project root, PostgreSQL DSN, service configs), and dispatches to the appropriate command handler. Commands that work without a project context (`init`, `projects`, `prune`) are dispatched before `Context::resolve()`.

`--verbose` is currently only consumed by `commands::symbols::outline()`, which
switches outline output from the slim projection to the full `Symbol` payload.

`gcode graph clear` and `gcode graph rebuild` are nested under a `graph`
subcommand group, but the existing read-side graph queries remain top-level:
`callers`, `usages`, `imports`, `path`, and `blast-radius`. `gcode path
<SYMBOL_A> <SYMBOL_B>` returns the shortest `CALLS` path between two resolved
symbol queries (`--max-depth` caps the hop count, default `8`).

## Configuration Resolution

**File:** `src/config.rs`

`Context::resolve(project_override, quiet)` orchestrates the full resolution flow:

### Project Detection

1. If `--project` is a directory path, use it directly
2. If `--project` is a name, look up in `code_indexed_projects` table (basename suffix match)
3. Otherwise, walk up from cwd looking for `.gobby/project.json` (Gobby-managed) or `.gobby/gcode.json` (standalone)
4. Otherwise, prefer the git worktree top-level (`git rev-parse --show-toplevel`, including linked worktrees — see "Worktree-aware project root" below)
5. Fall back to VCS root markers (`.git`, `.hg`) or cwd

The walk-up and `project.json` reading steps use `gobby_core::project::find_project_root` and `gobby_core::project::read_project_id` (extracted from `gcode/src/project.rs` so `ghook` and other binaries can share the same logic). Bootstrap-config and daemon-URL helpers also come from `gobby-core`. See [gobby-core Development Guide](gcore-development-guide.md) for the shared API.

### Project Identity

**File:** `src/config.rs` — `ProjectIdentity`, `ProjectIdentitySource`, `resolve_project_identity()`. **Companion type:** `IsolationMarker` (in `src/project.rs`).

After the project root is detected, `resolve_project_identity()` decides which `project_id` to use and which identity source produced it. This replaces the old "either `.gobby/project.json` or `.gobby/gcode.json`" binary with the five sources enumerated by `ProjectIdentitySource`:

| Source | Trigger | `project_id` derived from | Writes `.gobby/gcode.json`? |
|--------|---------|---------------------------|-----------------------------|
| `IsolatedRoot` | `IsolationMarker` present in `.gobby/project.json` (`parent_project_path` and/or `parent_project_id`), read via `project::read_isolation_marker` | UUID5 of canonical root path (`project::code_index_id_for_root`) | No |
| `LinkedWorktree` | `git::worktree_info()` reports `WorktreeKind::Linked` | UUID5 of the worktree top-level path | No |
| `ProjectJson` | `.gobby/project.json` exists, no `IsolationMarker` fields | `project_id` field from the file | No |
| `GcodeJson` | `.gobby/gcode.json` exists | `project_id` field from the file | No |
| `Generated` | None of the above | UUID5 of canonical root path | Only when caller passes `MissingIdentity::Generate` (i.e. `gcode init`) |

`MissingIdentity::Error` is the default for non-init commands — they fail with "Run `gcode init`" instead of silently creating a generated id. The `IsolationMarker` struct (`{parent_project_path: Option<String>, parent_project_id: Option<String>}`) is what makes the `IsolatedRoot` branch fire: a non-empty value in either field signals that the directory should keep its own filesystem-derived code-index id even though it carries a Gobby `project.json`. Linked-worktree resolution emits a warning when an inherited `project.json` id differs from the filesystem-derived id, so it's obvious that the latter is the one being used.

### `git` module — worktree detection

**File:** `src/git.rs`

A new dedicated module that owns all `git` shell-out logic for project-root detection. Public surface:

- `WorktreeKind` — enum: `Main`, `Linked`, `NotGit`.
- `WorktreeInfo` — `{kind: WorktreeKind, top_level: PathBuf, git_dir: PathBuf}`.
- `worktree_info(path) -> Result<WorktreeInfo>` — shells out to `git -C <path> rev-parse --show-toplevel` and `git rev-parse --git-dir`, then reads `git worktree list --porcelain` to classify the result as `Main`, `Linked`, or `NotGit`.

`Context::resolve` calls `worktree_info` during root detection so commands invoked deep inside a `git worktree add` directory resolve to the worktree's own top-level (not the main repo's `.git`-bearing directory). Failure modes degrade gracefully: if `git` is missing or the directory isn't a git repo, the call errors and the resolver falls back to the generic VCS-root markers.

### PostgreSQL Bootstrap

`src/db.rs` asks the local daemon broker for PostgreSQL DSNs first using
`POST /api/local/runtime/database-url` with `X-Gobby-Local-Token` from
`local_cli_token` and a 3s timeout. If the broker is unavailable, gcode falls
back to explicit fallback sources: `GCODE_DATABASE_URL`, `GOBBY_POSTGRES_DSN`,
`$GOBBY_HOME/gcore.yaml` `databases.postgres.dsn`, then
`$GOBBY_HOME/bootstrap.yaml` inline `database_url`. Bootstrap still requires
`hub_backend: postgres` when used.
`connect_readwrite()` and `connect_readonly()` both return a synchronous
`postgres::Client`; PostgreSQL permissions decide actual access.

`src/schema.rs` validates runtime schema and never creates or migrates tables.
It requires the Gobby hub tables, the `pg_search` extension, and BM25 indexes
`code_symbols_search_bm25` and `code_content_search_bm25`. Missing schema errors
tell users to configure the PostgreSQL hub with the required code-index schema.

### Service Configuration

Resolution order for infrastructure services (FalkorDB, Qdrant):

| Priority | Source | Example |
|----------|--------|---------|
| 1 (highest) | Environment variables | `GOBBY_FALKORDB_HOST`, `GOBBY_FALKORDB_PORT`, `GOBBY_FALKORDB_PASSWORD`, `GOBBY_QDRANT_URL` |
| 2 | `config_store` table in PostgreSQL | `databases.falkordb.host`, `databases.falkordb.port`, `databases.falkordb.password`, `databases.qdrant.url` |
| 3 (lowest) | Hardcoded defaults | FalkorDB port `16379` and graph name `gobby_code` once a host is configured |

AI capability config resolves from daemon-supported `config_store` keys first,
then standalone `~/.gobby/gcore.yaml`, then defaults. Embedding keys use the
canonical `ai.embeddings.*` namespace. `ai.text_generate.*` is a CLI
standalone/direct namespace; daemon text generation uses daemon provider config
such as `ai.generation.local.endpoints.<name>` and request-level provider/model
selection.

Config values are JSON-encoded in `config_store` — strings have surrounding quotes stripped. Secret patterns like `$secret:NAME` are resolved via `secrets.rs` (Fernet decryption using machine_id + salt, 600K PBKDF2 iterations).

### Runtime Model

gcode is daemon-independent but requires a configured PostgreSQL hub. Project
identity still comes from `.gobby/project.json`, `.gobby/gcode.json`, isolated
roots, linked worktrees, or generated identity during `gcode init`. Service
configuration comes from env vars first, then PostgreSQL `config_store`, then
defaults where the current behavior defines defaults.

## Indexing Pipeline

**Files:** `src/index/{walker,parser,chunker,hasher,indexer}.rs`

### Data Flow

```
walker::discover_files(root, excludes)
  → (ast_candidates, content_only_candidates)
    → parser::parse_file(path, project_id, root, excludes)
      → tree-sitter AST → extract_symbols + extract_imports + extract_calls
      → optional clangd semantic pass for C/C++ calls
      → link_parents (nest methods in classes, build qualified names)
    → chunker::chunk_file_content(source, rel_path, project_id, lang)
      → 100-line chunks with 10-line overlap
    → hasher::file_content_hash(path)
      → SHA-256 for incremental detection
    → indexer::upsert_symbols + upsert_file + upsert_chunks
      → PostgreSQL hub writes
      → upsert_imports + upsert_calls
      → graph_synced=false / vectors_synced=false for projection sync
```

### File Discovery (walker.rs)

Uses the `ignore` crate (`WalkBuilder`) which respects `.gitignore`,
`.git/info/exclude`, git global config, and the existing hidden-file behavior.
Files are partitioned through a shared classifier used by both directory
indexing and explicit `gcode index --files ...` indexing:

- **AST candidates**: Safe files where `languages::detect_language()` matches a tree-sitter language spec
- **Content-only candidates**: Safe non-secret, non-binary text files with no tree-sitter match — docs, skill files, configs, SQL/CSS, shell scripts, `Dockerfile`/`Makefile`, and extensionless text

Default excludes: `node_modules`, `__pycache__`, `.git`, `.venv`, `target`, `dist`, `build`, `.next`, `coverage`, etc.

Both candidate types obey path validation, symlink safety, binary detection
(8KB read), secret-name detection, empty-file rejection, the 10MB size limit,
and configured exclude patterns. Content-only files use the lowercase extension
as their language label, or `text` for extensionless files.

### Tree-Sitter Parsing (parser.rs)

1. **Security checks**: Path validation, symlink safety, binary detection (8KB read), secret file detection, 10MB size limit
2. **Language detection**: Extension → `languages::detect_language()` → `LanguageSpec`
3. **AST parsing**: Tree-sitter grammar per language, execute symbol/import/call queries
4. **Symbol extraction**: Captures `@name` and `@definition.KIND` from query results. Kind is parsed from the capture name (e.g., `definition.function` → kind `function`)
5. **Parent linking**: For each symbol, find the largest enclosing class/type by byte range. Nested functions in classes become `method` kind. Qualified names built as `parent_name.symbol_name`
6. **Signature**: First line of definition, truncated at 200 chars
7. **Docstring**: Python/JS/TS only — first string literal in function/class body

### Import Resolution and Call Target Classification

Import-resolution runs before call targets are finalized. It is intentionally
fail-closed: producers emit external targets only when import or module
provenance is explicit and structurally unambiguous.

- Local symbol calls resolve to canonical `callee_symbol_id` values
- Unmatched calls stay `unresolved`
- Import-bound external calls are marked `external` and retain their source module

Current external-callee support:

| Language | Safe external scope |
|----------|---------------------|
| Python | `import` / `from ... import ...` aliases for external modules |
| JavaScript / TypeScript | package imports from `package.json`, named/default/namespace imports, and `node:` builtins |
| Go | explicit import aliases and package selector calls; self-module imports remain unresolved |
| Rust | Cargo dependency/std roots, explicit or grouped `use` aliases, and path calls; glob imports and receiver methods remain unresolved |
| Java | explicit class imports and static imports; wildcard imports and instance calls remain unresolved |
| C# | using aliases, namespace-qualified calls, and single-source `using static`; instance/member inference remains unresolved |
| PHP | namespace `use`, `use function`, and fully qualified static/function calls; dynamic/member dispatch remains unresolved |
| Swift | direct module imports plus module-qualified calls; unqualified/member calls remain unresolved |
| Ruby | literal `require` roots from the curated stdlib/gem map plus constant-qualified calls; bare and receiver calls remain unresolved |
| Dart | explicit `as` aliases from `dart:` imports or external `package:` dependencies in `pubspec.yaml`; unaliased and relative imports remain unresolved |
| Elixir | curated Mix dependency roots plus explicit `alias` / `require`; `import`, `use`, and dynamic calls remain unresolved |
| C/C++ | optional clangd-backed semantic resolution when `clangd` and `compile_commands.json` are available; tree-sitter-only calls remain unresolved |

External resolution checks parameter and local variable shadowing before
classifying bare calls or qualified roots as external. Swift scoped imports
such as `import struct Foundation.Date` bind the module root (`Foundation`),
not the scoped keyword.

C/C++ semantic mode auto-enables when `clangd` is discoverable and
`compile_commands.json` exists at the project root or common build directories.
Set `GCODE_CLANGD` to override the clangd command and
`GCODE_COMPILE_COMMANDS_DIR` to override compile database discovery. Use
`gcode index --require-cpp-semantics` or `GCODE_REQUIRE_CPP_SEMANTICS=1` to
fail indexing when C/C++ semantic prerequisites are missing.

Research spikes in `docs/spikes/` define unresolved boundaries for C/C++ edge
cases and remaining external-callee work in Kotlin, Bash, Lua, Scala, Dart,
Elixir, and Ruby. This keeps unproven external package calls out of the local
call graph and improves FalkorDB correctness for `callers`, `usages`,
`blast-radius`, and graph-backed search.

### Language Support (languages.rs)

21 languages with tree-sitter queries for symbol definitions, imports, and call
sites where the grammar exposes a safe surface:

| Tier | Languages |
|------|-----------|
| Tier 1 | Python, JavaScript, TypeScript, Go, Rust, Java, C, C++, C#, Ruby, PHP, Swift, Kotlin, Scala, Objective-C |
| Tier 2 | Dart, Elixir, Lua, Bash |
| Tier 3 | JSON, YAML |

Each language has a `LanguageSpec` with three tree-sitter queries: `symbol_query`, `import_query`, `call_query`. Empty queries mean that feature is disabled for the language.

### Content Chunking (chunker.rs)

Files are split into overlapping chunks for BM25 content search:

- **Chunk size**: 100 lines
- **Overlap**: 10 lines (step = 90)
- **1-indexed**: line_start/line_end use 1-based indexing
- Empty/whitespace-only chunks are pruned

AST candidates and content-only candidates both write chunks. Content-only files
write `code_indexed_files` rows with `symbol_count=0`, so `tree`, `status`, and
`grep`/`search-content` see the broader repo text corpus without schema
changes. Markdown is content-only repo text and is outside tree-sitter AST
detection.

### Incremental Indexing (indexer.rs)

1. **Hash comparison**: SHA-256 content hash per file, stored in `code_indexed_files`
2. **Stale detection**: Compare current hashes against stored hashes; files with changed hashes are re-indexed.
3. **Orphan cleanup**: Files in the DB that no longer exist on disk have their FalkorDB code graph projection and Qdrant code-symbol vectors cleaned first, then their hub rows are deleted. Cleanup failures are recorded in `IndexOutcome.degraded`; PostgreSQL deletes still proceed.
4. **Per-file transactions**: PostgreSQL writes are wrapped in a single transaction to prevent half-indexed files on crash. Parsed files write `upsert_symbols` -> `delete_stale_file_symbols` -> `delete_file_non_symbol_facts` -> `upsert_file` -> `upsert_imports` / `upsert_calls` -> `upsert_content_chunks`, so unchanged-symbol summaries survive stale cleanup. Content-only files have no current symbol set to reconcile, so they still use full `delete_file_facts` before rewriting file and chunk rows.
5. **Projection sync flags**: Changed files are written with `graph_synced=false`, `vectors_synced=false`, `graph_sync_attempted_at=NULL`, and `vector_sync_attempted_at=NULL` so Rust projection sync can update FalkorDB and Qdrant code projections. Graph and vector sync mark the matching `*_attempted_at` timestamp before projection work and again on success. The daemon may schedule or delegate this work, but the projection writes live in `projection::sync`, `graph::code_graph`, and `vector::code_symbols`.

The `--full` flag skips the hash comparison and re-indexes all files, ensuring stale external index entries are cleaned up.

### Read-time Freshness

**File:** `src/freshness.rs`

Search, symbol, outline, and graph read commands gate their work on `freshness::ensure_fresh()` (project-scoped) or `freshness::ensure_symbol_fresh()` (single-symbol). Both verify that the on-disk source still matches the index and incrementally re-index affected files transparently before the read returns.

- **Scopes**: `FreshnessScope::Project` (re-runs the standard incremental indexer over the whole project root) and `FreshnessScope::Files(Vec<PathBuf>)` (limits the re-index to the listed files — used for `outline` and other file-scoped commands).
- **Symbol scope**: `ensure_symbol_fresh` looks up the symbol's source file and re-indexes just that file when the stored byte range no longer matches the on-disk content.
- **Re-entrancy guard**: `freshness::ensure_fresh` checks `GCODE_FRESHNESS_INFLIGHT`; if set, it short-circuits. The `FreshnessGuard` RAII helper sets and clears the same env var so the indexer's own subprocesses don't recurse into freshness.
- **Disable from CLI**: the global `--no-freshness` flag (`Cli::no_freshness`) skips all of the above. `main.rs` wraps each freshness call in `ensure_project_fresh` / `ensure_files_fresh` / `ensure_symbol_fresh` thin helpers that respect the flag.
- **Boundary**: freshness is per-call repair, not a substitute for `gcode index` after bulk changes (branch switches, large refactors). It's optimized for "this single file changed since the last index run."

### Project-scoped path validation

**File:** `src/commands/scope.rs`

Search, symbol, outline, and graph commands all run resolved paths through the helpers in `commands::scope` before returning results, so stale rows from other projects sharing the PostgreSQL hub cannot leak across project boundaries:

- `normalize_file_arg(ctx, file)` — relativize a CLI-supplied path against the resolved project root.
- `path_exists_in_current_project(ctx, file_path)` — does the path exist on disk inside the current project root?
- `indexed_file_exists` / `content_chunks_exist` — does the file have rows in `code_indexed_files` / `code_content_chunks` for the current `project_id`?
- `current_indexed_path_is_valid` — composite check used as a gate before returning results.

### Import/Call PostgreSQL Storage

When hub tables exist (`code_imports`, `code_calls`), gcode writes parsed relations during indexing:

- **`code_imports`**: `(project_id, source_file, target_module)` — from `parse_result.imports`
- **`code_calls`**: `(project_id, caller_symbol_id, callee_symbol_id, callee_name, callee_target_kind, callee_external_module, file_path, line)` — from `parse_result.calls`

Both use delete-then-insert per file. `callee_target_kind` distinguishes stable
local-symbol edges from unresolved or external calls, and
`callee_external_module` preserves package/module provenance for external calls.
The runtime schema validator requires these tables before gcode starts index/search work.

### Graph Lifecycle

Read-side graph queries go through `gobby_core::falkor::with_graph` /
`GraphClient`, then into gcode-owned query builders. `gcode graph overview`
accepts `--limit N`, which maps to the daemon's
`GET /api/code-index/graph?limit=...` contract when the daemon delegates overview
reads to `gcode`. Graph lifecycle operations are Rust-owned FalkorDB operations:

- `gcode graph clear` clears the current resolved project id.
- `gcode graph clear --project-id <PROJECT_ID>` clears by explicit project id before normal `Context::resolve()` and is the daemon stale-project cleanup path.
- `gcode graph sync-file --file <FILE>` syncs one existing `code_indexed_files`
  row into FalkorDB. It checks `code_indexed_projects` and `code_indexed_files`
  in PostgreSQL before any FalkorDB access. Missing project/file contract
  failures return typed JSON with exit code `2`; `--allow-missing-indexed-file`
  is reserved for daemon/background workers and turns only a missing indexed
  file into a skipped payload. Files with no imports, symbol definitions, or
  calls delete any stale file projection, mark `graph_synced=true`, and return
  `status: "skipped"` with `reason: "no_graph_facts"`.
- `gcode graph rebuild` clears and rebuilds the current resolved project id from PostgreSQL graph facts.
- `gcode graph cleanup-orphans` is the periodic graph-only reconciliation path
  (not run on every file sync). It deletes project-wide orphaned graph nodes —
  comparing `CodeFile.path`/`CodeSymbol.file_path` against PostgreSQL
  `code_indexed_files` — and then runs project orphan cleanup once.
- `gcode vector cleanup-orphans` is the Qdrant counterpart: it removes
  `code_symbols_{project_id}` vector points for files no longer indexed in
  PostgreSQL.
- `gcode prune` is the cross-project operator command. It removes stale projects
  and reconciles orphaned graph + vector projection state across the remaining
  indexed projects (keeps a confirmation prompt unless `--force`). `gcode
  --project <path-or-name> prune` scopes projection cleanup to one project, and
  graph/vector cleanup failures are reported independently.

Graph clear uses `MATCH (n {project: $project})` plus the code-index label
predicate (`CodeFile`, `CodeSymbol`, `CodeModule`, `UnresolvedCallee`,
`ExternalSymbol`). It must not target memory graph labels or bridge ownership.

### Graph Client Boundary

`src/falkor.rs` has been removed. gcode callers use `graph::code_graph` for
code-index graph reads and `gobby_core::falkor::GraphClient` for direct FalkorDB
access. Behavior tests cover the current graph contract:

- config resolution returns gcode's `FalkorConfig` with core connection fields
  plus `graph_name = "gobby_code"`;
- graph read APIs surface typed unavailable-service errors;
- query helpers preserve safe literal rendering, numeric clamping, and project
  scoping through returned query/param behavior;
- `gobby-core` exposes `GraphClient::with_sync_graph` for rare raw
  `SyncGraph` operations without duplicating connection setup.

Do not reintroduce direct FalkorDB builder/result imports in `gobby-code`.

### UUID5 Parity

Symbol IDs are deterministic UUID5 using namespace `c0de1de0-0000-4000-8000-000000000000` and key format `{project_id}:{file_path}:{name}:{kind}:{byte_start}`. This must match the Python daemon's `Symbol.make_id()` exactly — IDs are shared between gcode (Rust) and the Gobby daemon (Python).

## Search Pipeline

**Files:** `src/search/{fts,graph_boost,rrf}.rs`, `src/vector/code_symbols.rs`,
`src/commands/search.rs`, `src/commands/grep.rs`

### Hybrid Search (`gcode search`)

Four sources are queried and merged via Reciprocal Rank Fusion:

```
Source 1: pg_search BM25 (PostgreSQL full-text relevance)
  → search_symbols_fts (BM25 query on code_symbols)
  → required pg_search query; invalid BM25 input returns no BM25 source results

Source 2: Semantic (Qdrant vector search)
  → embed query text via an OpenAI-compatible /v1/embeddings endpoint
  → POST to Qdrant /collections/{name}/points/search
  → returns (symbol_id, score) pairs

Source 3: Graph Boost (FalkorDB)
  → find_callers + find_usages for query term
  → returns symbol IDs that are connected in the call/import graph

Source 4: Graph Expand (FalkorDB)
  → seed from top FTS + semantic hits
  → expand through callees first, then callers

  ↓ All sources → RRF merge → Symbol resolution → Positional path filters → Pagination
```

`search`, `search-symbol`, `search-text`, `search-content`, and `grep` accept
positional path filters after the query. Bare paths expand to exact + subtree
matches; glob paths stay verbatim; multiple paths use OR semantics. BM25
queries add a parenthesized SQL `LIKE` prefix OR block only when every expanded
pattern has a safe prefix. Rust `glob::Pattern` post-filtering then enforces
exact semantics across all sources, including semantic/graph results that lack
SQL-side filtering. If a path glob cannot be lowered to a SQL prefix, handlers
surface a hint and rely on post-query filtering after a broader fetch. `grep`
also accepts `-g/--glob`; glob filters compose with positional paths and are
applied before scanning indexed chunks.

### Indexed Grep (`gcode grep`)

`gcode grep <pattern> [PATH ...]` is exact indexed content search over
`code_content_chunks`, not a filesystem `rg` wrapper. It loads indexed chunks for
the current project, applies positional path and `-g/--glob` filters, scans lines
with either regex or `-F/--fixed-strings` matching, de-duplicates overlapping
chunks by `(file_path, line)`, and returns stable `file_path`, then line-number
ordering.

`-m/--max-count` caps matching lines globally. Context lines from `-C/-A/-B` do
not count toward that cap. JSON output is an envelope with `project_id`,
`pattern`, flags, `paths`, `globs`, `max_count`, `matched_lines`, `truncated`,
`scanned_chunks`, and matches containing `path`, `line`, `text`, `spans`,
`before`, and `after`. `--limit` is rejected so callers do not confuse ranked
search pagination with grep-style matching-line caps.

### RRF Merge (rrf.rs)

Reciprocal Rank Fusion with K=60:

```
score(symbol) = Σ 1/(K + rank) for each source containing the symbol
```

- Rank 0 is best (first in source list)
- Single-source max score: 1/60 ≈ 0.0167
- Multi-source: scores are additive across sources
- `gcode search` sorts by exact tier first, then RRF score; public `score`
  reflects that final display order and `rrf_score` preserves the raw RRF value.
- `gcode search-symbol --with-graph` uses the same RRF metadata for exact hits
  plus FalkorDB graph neighbors; default `search-symbol` stays exact-first
  without graph expansion.
- Source attribution is sorted deterministically (e.g., `["fts", "graph_expand"]`)

### Symbol Resolution

After RRF merge, ALL symbol IDs are resolved against PostgreSQL before computing `total`. This ensures `total` reflects the count of actually-resolvable symbols (stale Qdrant/FalkorDB entries are silently skipped). Offset/limit pagination is applied after resolution.

The `total` for hybrid search is a best-effort estimate bounded by `fetch_limit`
per source — exact counts aren't feasible because RRF merges results from
multiple systems with deduplication.

### BM25 Search (`search-text`, `search-content`)

These use dedicated `count_text`/`count_content` functions backed by pg_search BM25 counts for accurate totals when no positional path filters are present. With path filters, handlers fetch up to `FILTERED_FETCH_CAP`, apply glob filtering before pagination, and surface a hint if the cap is hit or a glob required post-query filtering.

Use `gcode search-content "query" [PATH ...]` for ranked content search. Use
`gcode grep "pattern" [PATH ...]` or `gcode grep "pattern" src -m 50` for exact
line-oriented indexed matches.

### Pagination

All search/graph commands return a `PagedResponse` envelope:

```json
{
  "project_id": "uuid",
  "total": 47,
  "offset": 0,
  "limit": 10,
  "results": [...],
  "hint": null
}
```

- `--offset N` skips the first N results
- `--limit N` caps results per page (default: 10)
- `--token-budget N` (on `search`, `usages`, and `blast-radius` only) trims
  returned rows to an approximate token ceiling, estimated as `ceil(chars/4)` per
  rendered row. Rows are trimmed from the tail after ranking/graph order is fixed;
  trimmed output populates `hint` in JSON and prints a `refine with ...` narrowing
  hint in text mode. It is not a global flag and is absent from `outline`/`symbol`.
- `hint` is populated when FalkorDB is unavailable (graph commands only)
- Text mode shows a pagination footer: `-- 10 of 47 results (use --offset 10 for more)`

## Database Schema

gcode uses Gobby's PostgreSQL baseline schema. Schema changes belong in the
Gobby repository migrations/baseline; gcode only validates and uses the tables.

### Core Tables

**`code_symbols`** — Extracted AST symbols

| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | UUID5 (deterministic, matches Python) |
| project_id | TEXT | Project identifier |
| file_path | TEXT | Relative to project root |
| name | TEXT | Symbol name |
| qualified_name | TEXT | `parent.name` for methods |
| kind | TEXT | function, class, method, type, etc. |
| language | TEXT | python, rust, etc. |
| byte_start, byte_end | INTEGER | Byte offsets for source extraction |
| line_start, line_end | INTEGER | 1-indexed line numbers |
| signature | TEXT | First line of definition (≤200 chars) |
| docstring | TEXT | Python/JS/TS only |
| parent_symbol_id | TEXT | Enclosing class/type ID |
| content_hash | TEXT | SHA-256 of symbol source |
| created_at, updated_at | TIMESTAMPTZ | Cast to text for CLI output |

**`code_indexed_files`** — File index metadata

| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | UUID5(project_id:file_path) |
| project_id | TEXT | |
| file_path | TEXT | Relative path |
| language | TEXT | Detected language |
| content_hash | TEXT | SHA-256 for incremental detection |
| symbol_count | INTEGER | |
| byte_size | INTEGER | |
| indexed_at | TIMESTAMPTZ | Cast to text for CLI output |
| graph_synced | BOOLEAN | `false` means FalkorDB sync pending |
| vectors_synced | BOOLEAN | `false` means Qdrant sync pending |

**`code_imports`** — Import relations (daemon migration v183, Gobby mode only)

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| project_id | TEXT | |
| source_file | TEXT | File that contains the import |
| target_module | TEXT | Imported module name |

UNIQUE constraint on `(project_id, source_file, target_module)`.

**`code_calls`** — Call relations (daemon migration v183, Gobby mode only)

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | Auto-increment |
| project_id | TEXT | |
| caller_symbol_id | TEXT | UUID5 of calling symbol |
| callee_symbol_id | TEXT | UUID5 of the resolved callee, or empty string |
| callee_name | TEXT | Display name of called symbol/function |
| callee_target_kind | TEXT | `symbol`, `unresolved`, or `external` |
| callee_external_module | TEXT | External package/module for `external` targets |
| file_path | TEXT | File containing the call |
| line | INTEGER | Line number of call site |

**`code_content_chunks`** — File content for BM25 search

| Column | Type | Notes |
|--------|------|-------|
| id | TEXT PK | UUID5(project_id:file_path:chunk:N) |
| project_id | TEXT | |
| file_path | TEXT | |
| chunk_index | INTEGER | Sequential per file |
| line_start, line_end | INTEGER | 1-indexed, inclusive |
| content | TEXT | Chunk text (100 lines, 10-line overlap) |
| language | TEXT | |

**`code_symbols_search_bm25`** / **`code_content_search_bm25`** — pg_search BM25 indexes for full-text relevance

**`code_indexed_projects`** — Project statistics

**`config_store`** — Key-value configuration (Gobby mode only)

## FalkorDB Graph Model

### Nodes

- **CodeSymbol**: `{id, name, kind, project, line_start}`
- **UnresolvedCallee**: `{id, name, project}` placeholder for unresolved call targets
- **ExternalSymbol**: `{id, name, module, project}` placeholder for external package/module calls
- **CodeFile**: `{path, project}`
- **CodeModule**: `{name}`

### Edges

- **DEFINES**: `CodeFile -[:DEFINES]-> CodeSymbol` (file defines symbol)
- **CALLS**: `CodeSymbol -[:CALLS {file, line}]-> (CodeSymbol|UnresolvedCallee|ExternalSymbol)` (call relationship)
- **IMPORTS**: `CodeFile -[:IMPORTS]-> CodeModule` (import relationship)

### Query Patterns

- **Callers**: `MATCH (caller)-[:CALLS]->(target {id, project}) RETURN caller`
- **Usages**: `MATCH (source:CodeSymbol)-[:CALLS]->(target {id, project}) RETURN source`
- **Imports**: `MATCH (f:CodeFile {path, project})-[:IMPORTS]->(m:CodeModule)`
- **Blast Radius**: Variable-length `CALLS` traversal with depth limit. Returns `affected.line_start AS line` for symbol line numbers.

Count queries use the same patterns with `RETURN count(...)` for accurate pagination totals.

### Symbol Name Resolution

**File:** `src/search/fts.rs` (`resolve_graph_symbol`), `src/commands/graph.rs` (`resolve_symbol`)

All graph commands resolve user input to an actual symbol name before querying FalkorDB. Resolution order:

1. **Exact match** — `SELECT ... FROM code_symbols WHERE id/name/qualified_name = $1`
2. **LIKE match** — `WHERE name LIKE $pattern OR qualified_name LIKE $pattern`
3. **BM25 search** — `search_symbols_fts()` across names, signatures, docstrings, summaries

Single match → used directly. Multiple matches → fail closed with alternatives shown on stderr. No match → error message, no FalkorDB query issued.

## Semantic Search Integration

**File:** `src/vector/code_symbols.rs`

### Embedding Request

- **Endpoint**: OpenAI-compatible `/v1/embeddings`
- **Config**: `ai.embeddings.api_base`
- **Model**: `ai.embeddings.model` / default `nomic-embed-text`
- **Auth**: optional bearer token via `ai.embeddings.api_key`
- **Task prefix**: `search_query: ` for query embeddings

### Vector Lifecycle

1. **Index**: PostgreSQL writes mark files dirty for external sync; `gcode index --sync-projections` handles vector upserts when Qdrant and embeddings are configured
2. **Re-index**: stale file cleanup deletes Qdrant points filtered by `project_id` and `file_path` before hub facts are removed
3. **Search**: embed query text, search Qdrant, return `(symbol_id, score)` pairs

Project vector clear/rebuild targets only the `code_symbols_{project_id}`
collection and filters by `project_id`; it must not list, drop, or mutate memory
vector collections.

### Collection Naming

`{collection_prefix}{project_id}` — default prefix is `code_symbols_` from Qdrant config.

## Graceful Degradation

Each external service degrades independently:

| Service | When Unavailable | Impact |
|---------|-----------------|--------|
| FalkorDB | No config or connection refused | Graph commands return `[]` with hint; search loses graph boost |
| Qdrant | No URL configured | Search loses semantic source; BM25 still works |
| Embeddings API | No API base, auth failure, or request error | Semantic search disabled for that query |
| Daemon | Not running | Normal index/search and configured graph/vector lifecycle still work; daemon automation is unavailable |
| PostgreSQL hub | Missing bootstrap, non-postgres backend, unreachable DB, or missing schema | Runtime index/search commands fail clearly |

The system always works without the daemon process once the PostgreSQL hub is configured with the required schema.

## Output Format

### Default (Slim)

- **Search**: `id`, `name`, `qualified_name`, `kind`, `file_path`, `line_start`, `score`, `signature`, `sources`
- **Outline**: `id`, `name`, `kind`, `line_start`, `line_end`, `signature` (6 fields vs full Symbol)
- **Graph**: `id`, `name`, `file_path`, `line`, `relation`, `distance`

### Verbose (`--verbose`)

- **Outline**: Returns full `Symbol` struct with all fields

Fields never shown even in verbose: `content_hash`, `created_at`, `updated_at`, `byte_start`, `byte_end`. `project_id` is hoisted to the `PagedResponse` envelope.

## Use Cases

### Agent Search Workflow

1. `gcode search "auth middleware"` → ranked results with file:line, signature
2. Pick result → `gcode symbol <id>` (exact source code) or `gcode symbol-at file_path:line_start`
3. Need more? → `gcode search "auth middleware" --offset 10`

### File Survey

1. `gcode outline src/config.rs` → slim symbol list (name, kind, lines, signature)
2. If you already have a file and line, use `gcode symbol-at src/config.rs:42`
3. If navigating by structure or ID, identify relevant functions → `gcode symbol <id>` for source code

### Impact Analysis

1. `gcode blast-radius "handleAuth" --depth 3` → transitive dependents
2. `gcode callers "handleAuth"` → direct call sites
3. `gcode usages "DatabasePool"` → all references (calls + imports)

### Cross-Project Queries

```bash
gcode search --project myapp "query"        # by folder name
gcode search --project /path/to/myapp "query"  # by path
```

### Full Reindex

```bash
gcode index --full    # re-process all files, clean stale vectors
gcode invalidate      # destructive reset of current project's code-index rows
```

`gcode invalidate` is project-scoped: PostgreSQL deletes are filtered by the
resolved project id, FalkorDB cleanup targets nodes with that project id, and
Qdrant cleanup targets only `code_symbols_{project_id}`.

`gcode setup --standalone --overwrite-code-index` is the full standalone
code-index reset. It drops/recreates only allowlisted gcode PostgreSQL
relations and BM25 indexes, clears code-index graph labels in FalkorDB, and
deletes Qdrant collections with the `code_symbols_` prefix. Default standalone
setup fails on incompatible existing code-index state and prints the overwrite
rerun guidance.

### Code Documentation Generation

`gcode codewiki` generates vault-ready hierarchical code documentation
(architecture/module/per-file Markdown pages with grounded `[file:line]`
citations) from the same indexed facts. `--scope <PATH>...` limits the doc set,
`--since <GIT_REF>` drives incremental regeneration of only the pages whose
sources or cross-file neighbors changed, and `--repair-citations` re-anchors
existing citations against the current index without any AI calls. AI prose is
tuned with `--ai`, `--ai-depth`, `--ai-prose-depth`, and `--ai-register`. By
default it documents code and structured config; `--include-docs` extends it to
content-only Markdown/text.

_Last verified: 2026-06-23_

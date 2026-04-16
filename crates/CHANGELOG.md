<!-- markdownlint-disable MD024 MD013 -->

# Changelog

All notable changes to gobby-cli are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] — gcode

### Changed

#### gcode

- **gobby-core integration** — Migrated to consume the new `gobby-core` crate for project-root walk-up, bootstrap-config resolution, and daemon-URL construction. Inline implementations in `src/project.rs` removed (-109 lines); `src/config.rs` and `src/commands/index.rs` now use the shared helpers. No user-visible behavior change. (#115)

### Fixed

#### gcode

- **FTS LIKE escape** — Hardened FTS5 LIKE-clause escape in `src/search/fts.rs` against patterns containing `%`, `_`, or `\`. Prevents pathological queries from matching unintended rows. (#118)
- **graph.rs dedup** — Deduplicated unresolved-symbol response building in `src/commands/graph.rs` (-63 lines net). No behavior change. (#118)

#### CI/CD

- **Binary-specific tag prefixes** — Release workflows now trigger on `gcode-v*`, `gsqz-v*`, `gloc-v*`, `gcore-v*`, and `ghook-v*` tags so each crate releases independently. (#110)
- **Release gating** — Added `release-gobby-core` workflow; GitHub releases for binary crates are gated on successful crates.io publish. (#116)

## [0.4.1] — gsqz

### Fixed

#### gsqz

- **Low-savings marker** — Suppress `[gsqz:low-savings]` marker when prepending it would grow the output beyond the original. The marker now only annotates when the annotation itself doesn't make things worse. (#111)
- **Outer compression header for `/no-op` strategy** — When the low-savings marker is suppressed (above), the resulting `{pipeline}/no-op` strategy now also skips the outer `[Output compressed by gsqz — …, 0% reduction]` header and the daemon savings report. The user sees the original output verbatim. `CompressionResult::is_passthrough()` classifies `passthrough`, `excluded`, and `*/no-op` together so both call sites stay in sync. (#121)

## [0.1.0] — gobby-hooks

### Added

#### gobby-hooks

- **Initial release** — Sandbox-tolerant hook dispatcher binary `ghook`. Spools envelopes to `~/.gobby/hooks/inbox/` *before* POSTing to the local Gobby daemon, so the daemon's drain worker can replay deliveries lost to sandbox FS-read denials, network blips, or daemon restarts. (#114)
- **Dispatch mode** — `ghook --gobby-owned --cli=<c> --type=<t> [--critical] [--detach]` reads stdin, enriches with terminal context where applicable, atomically writes the envelope, then attempts the daemon POST.
- **Diagnose mode** — `ghook --diagnose --cli=<c> --type=<t>` prints a JSON snapshot of what would happen — daemon URL, project root/id, recognized CLI, critical flag, terminal-context preview. No network, no envelope write. Output validated against `schemas/diagnose-output.v1.schema.json`.
- **Version mode** — `ghook --version` prints the version and writes `~/.gobby/bin/.ghook-compatibility` so the daemon can detect schema-version mismatches.
- **Exit codes** — `0` for delivered or non-critical failure (envelope still enqueued); `2` for critical failure (envelope enqueued; signals the host CLI to abort).
- **Schemas** — `inbox-envelope.v1.schema.json` and `diagnose-output.v1.schema.json`, both validated against the Rust types in unit tests.
- **Host CLI registry** — Out-of-the-box support for `claude`, `codex`, `gemini`, `qwen` (per-CLI critical-hooks set + terminal-context-hooks set). Unknown CLIs are tolerated — envelope still spools, just without enrichment.
- **Quarantine** — Malformed stdin lands in `~/.gobby/hooks/inbox/quarantine/` as a body+meta pair. The drain never replays quarantined envelopes; they surface via `gobby status` and daemon logs.
- **Atomic spool writes** — Envelopes use write-temp + `fsync` + rename, so the drain only ever sees fully-written files.
- **Renamed for consistency** — Crate renamed from `gobby-hook` to `gobby-hooks`; binary stays `ghook`. (#117)

## [0.1.0] — gobby-core

### Added

#### gobby-core

- **Initial release** — Shared-primitives crate consumed by `gcode`, `gsqz`, `gloc`, and `ghook`. Three modules:
  - `project` — walk up from cwd to find `.gobby/project.json` (or legacy `.gobby/gcode.json`), read `id`/`project_id`.
  - `bootstrap` — resolve `~/.gobby/bootstrap.yaml` into a `DaemonEndpoint` (host + port). Falls back to `127.0.0.1:60887` on any failure.
  - `daemon_url` — compose a dial URL from a `DaemonEndpoint`, normalizing wildcard listen addresses (`0.0.0.0`, `::`, `::0`) to `127.0.0.1`.
- **Extracted from inline implementations** in the binary crates so behavior changes propagate with one PR instead of four. (#112, #113, #117)

## [0.4.0] — gsqz

### Added

#### gsqz

- **`replace` step** — line-by-line regex substitution with backreference support (`$1`, `$2`). Rules chain sequentially. Useful for normalizing paths, version strings, timestamps before other steps run (#97)
- **`match_output` step** — short-circuit step that checks the full output blob against regex rules. If a pattern matches (and optional `unless` doesn't), returns a short message immediately, skipping remaining steps. Used in test pipelines to return "All tests passed." when no failures detected (#98)
- **`on_empty` fallback** — per-pipeline and global `on_empty` message when steps produce empty output. Pipeline-level overrides global. Prevents confusing empty responses (#100)
- **Degradation markers** — `[gsqz:passthrough]` prepended when no pipeline matched (fallback used), `[gsqz:low-savings]` when a named pipeline achieves less than 5% savings. Tells the LLM about output quality (#99)
- **Compound command splitting** — splits `&&`, `||`, `;` while respecting quotes and parentheses. Tries segments in reverse (last command's output is most relevant) for pipeline matching. Pipes (`|`) are not split (#101)
- **`gsqz input` subcommand** — prose compression from stdin with `--level lite|standard|aggressive`. Strips filler phrases (24 rules) and filler words while preserving code blocks, YAML frontmatter, inline code, URLs, XML tags, file paths, and headings (#108)
- **`gsqz output` subcommand** — explicit form of existing `gsqz -- <command>` behavior. Bare `gsqz -- <command>` preserved for backward compatibility (#108)
- **`compress_prose` pipeline step** — use prose compression as a pipeline step in YAML config (`compress_prose: { level: standard }`)
- **`Config::builtin()`** — test helper for deterministic config loading (avoids `~/.gobby/gsqz.yaml` override)

## [0.5.3]

### Fixed

#### gcode

- **Release workflow** — remove stale `--features embeddings` and `--no-default-features` flags from release-gcode.yml. Embeddings feature was removed in 0.5.2 but the workflow wasn't updated (#106)

#### CI/CD

- **gcode release** — remove cmake install step and per-platform feature matrix, now builds identically on all targets

## [0.1.1] — gloc

### Fixed

#### gloc

- **Windows build** — add `#[cfg(unix)]` / `#[cfg(not(unix))]` gate on `exec_client()`. Unix uses `exec()` (replaces process), Windows falls back to `Command::status()` (spawns child, exits with its code) (#106)

## [0.1.0] — gloc

### Added

#### gloc

- **Initial release** — local LLM launcher for AI CLI tools (#102)
- **Backend auto-detection** — probes LM Studio (`localhost:1234`) and Ollama (`localhost:11434`) in config order, first responding wins
- **Ollama model management** — checks downloaded models via `/api/tags`, pulls missing models via `/api/pull` (when `auto_pull: true`), warms up models via `/api/generate` (when `auto_load: true`)
- **LM Studio JIT support** — no explicit model loading needed, LM Studio loads on first request
- **Multi-client support** — Claude Code and Codex CLI out of the box, extensible via config
- **Template env vars** — `{backend.url}`, `{backend.auth_token}`, `{backend.name}`, `{model}` resolved per client
- **Model aliases** — shorthand names mapped to full model identifiers (e.g. `qwen` -> `qwen3-coder`)
- **Layered YAML config** — `--config` > `.gobby/gloc.yaml` > `~/.gobby/gloc.yaml` > built-in default (same pattern as gsqz)
- **`--init` flag** — writes default config to `.gobby/gloc.yaml` with backup of existing
- **`--status` mode** — displays detected backend, client, model, env vars, and resolved binary path without launching
- **`--dump-config`** — prints resolved config summary
- **`exec()` semantics** — replaces the gloc process with the client binary (no wrapper overhead)
- CLI flags: `--client`, `--backend`, `--model`, `--url`, `--config`

## [0.3.2]

### Fixed

#### gsqz

- **Config resolution order** — fixed config search to check project (`.gobby/gsqz.yaml`) then global (`~/.gobby/gsqz.yaml`) correctly (#93)
- **Auto-export location** — auto-export writes to `~/.gobby/gsqz.yaml` (global), `--init` writes to `.gobby/gsqz.yaml` (project) (#94)

## [0.5.2]

### Fixed

#### gcode

- **Suppress llama.cpp stderr noise** — redirect fd 2 to `/dev/null` during embedding model init to suppress "embeddings required but some input tokens were not marked as outputs" warnings from llama.cpp's C layer (#87)
- **Empty search feedback** — `search`, `search-text`, and `search-content` now print "No results." to stderr in text format when the result set is empty. Previously text format produced no output, making it impossible to distinguish zero results from a failure (#88)

## [0.3.1]

### Changed

#### gsqz

- **Simplified config loading** — replaced 4-layer merge system (built-in → global → project → CLI) with simple priority: `--config` flag → `./gsqz.yaml` → compiled-in default. First found wins, no merging (#89)
- **Auto-export config** — on first run, if no `./gsqz.yaml` exists, gsqz creates one from the built-in default so users can edit immediately (#90)
- **`--init` flag** — writes the default config to `./gsqz.yaml`. If the file already exists, renames it to `gsqz.yaml.bak` first (#90)
- **Config error handling** — malformed YAML now prints a parse error and suggests `gsqz --init` instead of silently falling back (#90)
- **Removed git truncation** — `git-diff`, `git-log`, `git-transfer`, and `git-mutation` pipelines no longer truncate output. Filter and group steps remain (#86)

## [0.5.1]

### Added

#### gcode

- **Symbol name resolution for graph commands** — `callers`, `usages`, and `blast-radius` now resolve fuzzy input before querying Neo4j. Resolution tries exact match → LIKE substring match → FTS5 search (names, signatures, docstrings). When multiple matches are found, the best is used and alternatives are shown. When no match is found, a clear "No symbol matching" message is printed (#80)
- **`line_start` on Neo4j CodeSymbol nodes** — `write_defines` now stores `line_start` on CodeSymbol nodes. `blast-radius` Cypher returns `affected.line_start AS line` for non-zero line numbers in output (requires re-index to populate) (#80)

### Changed

#### gcode

- **Progress bar redesign** — static-width layout with bar on left, counter and filename on right: `[████░░░░░░░░] 21/42 : path/to/file.rs`. No more jumping as path lengths change (#83)

## [0.5.0]

### Changed

#### gcode

- **Defer external writes to daemon** — when the Gobby daemon is available (`daemon_url` resolved and `graph_synced` column detected), `gcode index` now performs SQLite writes only and skips all Neo4j/Qdrant operations. Sync flags (`graph_synced`, `vectors_synced`) stay at `0` for the daemon's background worker to process. FTS search works immediately; graph/semantic search follows once the daemon syncs. Standalone mode is unchanged (#78)
- `GraphSyncPending` files are skipped during incremental indexing when daemon is available — the daemon worker handles retries instead of gcode (#78)
- Orphan file cleanup defers Neo4j/Qdrant deletion to the daemon's `reconcile_stores` when daemon is available (#78)
- Qdrant collection creation skipped when daemon handles external sync (#78)

### Added

#### gcode

- **Import/call relation SQLite storage** — `gcode index` now writes parsed import relations (`code_imports` table) and call relations (`code_calls` table) to SQLite when the tables exist (daemon migration v183). Enables the daemon to rebuild Neo4j graph edges without re-parsing files. Table detection via PRAGMA means no deployment ordering required (#78)
- `vectors_synced` column support — detected at runtime alongside `graph_synced`, set to `0` on file upsert. Allows independent tracking of Qdrant vector sync status vs Neo4j graph sync (#78)
- `gcode kinds` command — lists all distinct symbol kinds in the current project index (#76)
- Context-aware CLI help — graph commands (`callers`, `usages`, `imports`, `blast-radius`) marked `[requires Gobby]` in help text. `search` describes optional semantic/graph sources. `index` notes SQLite-only behavior when daemon is running. Commands grouped by mode requirements (#77)
- Generic `has_column()` and `has_table()` helpers replacing the single-purpose `has_graph_synced_column()` (#78)

### Fixed

#### gcode

- Fix cross-project symbol contamination when `gcode index <path>` targets a different project than CWD — re-resolves project ID and DB path from the target path instead of using CWD-derived context. Prints a warning when context is re-resolved (#75)
- Fix bogus "saved 100%" output when outline/symbol returns empty results — skip savings reporting when actual bytes is zero (#74)

## [0.4.5]

### Fixed

#### gcode

- Fix cross-project symbol contamination when `gcode index <path>` targets a different project than CWD — re-resolves project ID and DB path from the target path instead of using CWD-derived context. Prints a warning when context is re-resolved (#75)

## [0.4.4]

### Fixed

#### gcode

- Fix bogus "saved 100%" output when outline/symbol returns empty results — skip savings reporting when actual bytes is zero (#74)

## [0.4.3]

### Changed

#### gcode

- Replace direct-SQLite savings tracking with HTTP POST to the Gobby daemon — `record_savings(conn, ...)` replaced by `report_savings(base_url, ...)` hitting `/api/admin/savings/record`, matching the gsqz pattern. Adds `ureq` dependency; daemon handles token conversion (#71)

## [0.4.2]

### Added

#### gcode

- `--path <glob>` filter for `search`, `search-text`, and `search-content` — scopes results to files matching a glob pattern (e.g. `--path "src/**/*.rs"`). Uses SQL LIKE prefix pre-filter for index-assisted narrowing with Rust `glob::Pattern` post-filter for exact semantics (#67)
- Restore `summary` field to `Symbol` and `SearchResult` models — the daemon writes summaries to `code_symbols` in `gobby-hub.db`; gcode now reads and surfaces them in search results. Upsert SQL deliberately omits summary to preserve daemon-written values (#68)

### Fixed

#### gcode

- Fix UTF-8 boundary panic in `symbol_embed_text_with_source` — byte-level truncation at 300/500 could land inside multi-byte characters (e.g. box-drawing chars). Uses `floor_char_boundary` polyfill for MSRV 1.85 compatibility (#66)

### Changed

#### gcode

- Refactored FTS search functions to use dynamic parameter builders instead of branch-per-filter pattern, enabling clean composition of optional `--kind` and `--path` filters (#67)

## [0.4.1]

### Added

#### gcode

- Two-pass graph expansion for hybrid search — after FTS+semantic return results, top symbol names are used to batch-query Neo4j for callers and callees, feeding a 4th RRF source (`graph_expand`) that surfaces structurally related symbols (#62)
- `find_callers_batch` and `find_callees_batch` Neo4j functions with `IN $names` Cypher for efficient batch graph queries (#62)
- Body snippet enrichment for symbol embeddings — `symbol_embed_text_with_source` appends first 300 chars of function body to embedding text, improving semantic search for conceptual queries (#64)
- Source bytes carried through `ParseResult`, eliminating double file read during indexing (#64)

### Changed

#### gcode

- `graph_expand` takes `&[String]` slice reference instead of owned `Vec<String>` (#64)
- `find_callees_batch` uses correct `callee_id`/`callee_name` Cypher aliases; `row_to_graph_result` updated with callee fallback chain (#64)

## [0.4.0]

### Added

#### gcode

- Per-file SQLite transactions for crash-safe indexing — prevents half-indexed files if the process is killed mid-index (#57)
- `graph_synced` column support — detects column in `gobby-hub.db` at runtime, sets to `0` on file upsert, flips to `1` after successful Neo4j/Qdrant writes. Incremental index auto-detects unsynced files and retries external writes only (#57)
- `StaleReason` enum (`ContentChanged` / `GraphSyncPending`) for smarter incremental detection (#57)

#### gsqz

- `SKILL.md` — standalone skill file that teaches AI agents to wrap verbose Bash commands with `gsqz --` for token-optimized output compression (#51)

### Changed

#### gcode

- **Breaking:** Remove `gcode summary` command — the Gobby SymbolSummarizer is being removed; summaries were never generated by gcode (#58)
- Remove `summary` field from `Symbol` and `SearchResult` models (#58)
- Remove `summary` from `upsert_symbols` SQL (#58)
- Remove `verbose` parameter from `search` command (only controlled summary display) (#58)
- Move `repo_outline` from `commands/summary.rs` to `commands/status.rs` (#58)
- Neo4j write functions (`write_defines`, `write_calls`, `write_imports`, `delete_file_graph`) now return `Result<()>` instead of silently discarding errors (#56)
- Neo4j writes in `index_file` are invoked independently instead of short-circuiting — all three are attempted even if one fails (#60)

### Fixed

#### gcode

- Fix `gcode invalidate` returning 404 from daemon — notify daemon before clearing SQLite so it can still read project stats (#52)
- Fix Neo4j short-circuit write chain preventing `write_calls`/`write_imports` from running after `write_defines` failure (#60)

## [0.3.3]

### Fixed

#### gcode

- Fix `gcode invalidate` returning 404 from daemon — notify daemon to clean Neo4j/Qdrant **before** deleting from SQLite, so the daemon can still read project stats from the shared `gobby-hub.db` (#52)

## [0.3.2]

### Added

#### gcode

- `--offset` flag on `search`, `search-text`, `search-content`, `callers`, `usages` for stateless pagination (#43)
- `--full` flag on `index` to force non-incremental reindex, cleaning up stale external indices (#43)
- `PagedResponse` envelope on all paginated JSON commands: `{ project_id, total, offset, limit, results, hint }` (#43, #45)
- Text mode pagination footer: `-- 10 of 47 results (use --offset 10 for more)` (#43)
- Accurate `total` counts via FTS5 COUNT queries for `search-text` and `search-content` (#44)
- Neo4j server-side COUNT queries (`count_callers`, `count_usages`) for accurate graph pagination totals (#45)
- Neo4j server-side SKIP/LIMIT for `find_callers` and `find_usages` — no more over-fetch and skip in Rust (#48)
- Empty-offset messaging across all search and graph commands (#48)

#### Documentation

- Development guides for gcode and gsqz — architecture, data flow, internals, design decisions (#46)

### Changed

#### gcode

- Default `--limit` reduced from 20 → 10 on search/callers/usages commands (#43)
- `outline` JSON output uses slim `OutlineSymbol` struct (6 fields vs 18) — full output via `--verbose` (#43)
- `search` JSON output drops `summary` by default — include via `--verbose` (#43)
- `project_id` hoisted to response envelope instead of repeating on every result (#43)
- Graph commands (`callers`, `usages`, `imports`, `blast-radius`) all use `PagedResponse` consistently — removed ad-hoc JSON hint wrapper (#45)
- `:CodeSymbol` label added to `find_usages` and `count_usages` target node for consistent Neo4j query planning (#48)

## [0.3.1]

### Fixed

#### gcode

- Fix stale Qdrant vectors causing "failed to look up symbol" warnings during search — `delete_file_data` now cleans up Qdrant vectors alongside SQLite and Neo4j when re-indexing files (#38)
- Suppress noisy stderr warnings for stale external index entries — silently skipped instead (#38)

## [0.3.0]

### Changed

#### gcode

- **Breaking (build):** Metal GPU acceleration is now platform-conditional — automatically enabled on macOS, absent on other platforms. Previously, `embeddings` always pulled in Metal, which failed to build on Linux/Windows (#30)
- Split release workflow into per-crate workflows (`release-gcode.yml`, `release-gsqz.yml`) — both still trigger on `v*` tags (#31)

### Added

#### gcode

- `cuda` feature flag — opt-in NVIDIA GPU acceleration for embeddings on Linux/Windows (requires CUDA toolkit) (#32)
- `vulkan` feature flag — opt-in cross-vendor GPU acceleration for embeddings on Linux/Windows (requires Vulkan SDK) (#32)
- `rocm` feature flag — opt-in AMD GPU acceleration for embeddings on Linux (requires ROCm stack) (#32)

#### Platform Support

- Linux and Windows can now build with embeddings enabled (CPU inference by default)
- GPU acceleration available via `--features cuda`, `--features vulkan`, or `--features rocm`

## [0.2.8]

### Added

#### gcode

- Index coverage tracking: `gcode status` and `gcode projects` now show file coverage percentage (e.g. `39/58 (67%)`) to help agents decide whether to use the index or grep (#27)
- `total_eligible_files` cached during `gcode index` runs — no extra disk I/O on status queries
- Schema migration (v1→v2) for standalone `gobby-code-index.db`

## [0.2.7]

### Added

#### gcode

- `gcode prune` command to detect and remove stale project entries (dead paths, relative paths, sentinel UUIDs) with daemon Neo4j/Qdrant cleanup notification (#25)

### Improved

#### gcode

- `gcode projects` and `gcode status` text output now shows friendly project names (basename + short UUID) and human-readable timestamps (#25)
- Timestamps normalized across epoch seconds and ISO 8601 formats to consistent `YYYY-MM-DD HH:MM:SS UTC` display (#25)

## [0.2.6]

### Fixed

#### gcode

- Fix empty `GOBBY_PORT` env var blocking daemon URL fallback — treat empty string same as unset (#22)
- Move `GGML_METAL_TENSOR_ENABLE` env var to top of `main()` before any threads spawn — setting env vars during lazy init was undefined behavior on macOS due to concurrent reads

## [0.2.5]

### Fixed

#### gcode

- Fix Metal GPU crash on pre-M5 Apple Silicon (M1-M4) caused by GGML residency set cleanup bug in non-tensor codepath — force-enable tensor API via `GGML_METAL_TENSOR_ENABLE` env var (#18)
- Fix Metal residency set assertion crash on process exit — explicitly drop embedding model before static destructor teardown (#18)
- Fix daemon URL fallback returning `None` when bootstrap.yaml has no `bind_host`, and normalize trailing slashes (#16)
- Fix Qdrant collection not created during `gcode index` — add `ensure_collection` to auto-create with correct vector config when Gobby is installed (#20)

### Added

#### gcode

- `--verbose` global flag to enable GGML/llama.cpp debug output (suppressed by default to save agent tokens) (#19)
- `--version` flag for gsqz CLI (#17)

## [0.2.4]

### Fixed

#### gcode

- Fix `root_path` not updated on re-index — `upsert_project_stats` was missing `root_path` in the `ON CONFLICT DO UPDATE` clause (#10)

### Added

#### gcode

- `gcode invalidate` now notifies the Gobby daemon to clean Neo4j graph nodes and Qdrant vectors via `POST /api/code-index/invalidate` (#11)
- Daemon URL resolved from `~/.gobby/bootstrap.yaml` (`daemon_port` + `bind_host`) instead of hardcoded default (#12)
- Migrate config_store keys from `memory.*` to `databases.neo4j.*` and `databases.qdrant.*` namespace (#15)

## [0.2.3]

### Fixed

#### gcode

- Fix startup hang caused by llama-cpp-2 v0.1.140 C++ static constructors blocking before main() on macOS Metal — update to v0.1.141 (#9)
- Wire up batch `embed_texts` in indexing pipeline instead of one-at-a-time `embed_text` calls (#9)
- Remove unused `embed_texts` export warning (#9)

## [0.2.2]

### Fixes

#### gsqz

- Fix dedup group transition losing representative line before repeat marker (#6)
- Fix truncate omission marker having extra leading newline (#6)
- Update README badge and download URLs from old GobbyAI/gsqz repo (#6, #7)
- Fix cargo install command to target `gobby-squeeze` crate (#7)

#### gcode

- Fix `symbols` command panic when stale index has byte_start beyond file length (#6)
- Replace `process::exit(1)` with proper error returns in `summary` and `symbol` commands (#6)
- Return `Result` from `symbol_content_hash` instead of panicking on invalid ranges (#6)
- Use safe `try_into()` for i64→usize casts in symbol deserialization (#6)
- Log database lookup errors in search instead of silently swallowing (#6)
- Use bounded 8KB read in `is_binary` instead of reading entire file (#6)
- Fix UTF-8 multi-byte panic in progress bar path truncation (#6)
- Add missing Swift `LanguageSpec` to match existing tree-sitter parser (#6)

### Improvements

#### gcode

- Rename misleading `iso_now` to `epoch_secs_str` in chunker and indexer (#6)
- Add `#[serial_test::serial]` to config tests that read environment variables (#6, #7)
- Fix `test_config_defaults` to actually test `resolve_neo4j_config` defaults (#6)
- Set `rust-version = "1.85"` in both crate manifests (#6)

#### Documentation

- Add `text` language specifier to fenced code blocks in user guides (#6)

## [0.2.1]

### Fixes

#### gsqz

- Fix ripgrep output compression mangling results and making them unreadable (#2)
- Fix pytest warnings being hidden in compressed output (#3)
- Fix git-diff compression losing meaningful context (#4)

### CI/CD

- Add `cargo publish` step to release workflow for crates.io publishing

## [0.2.0]

### Features

#### Cargo Workspace

- Consolidated `gcode` and `gsqz` into a single Cargo workspace under `gobby-cli` (#28)
- Unified CI pipeline: single `ci.yml` tests both crates, single `release.yml` builds and publishes both binaries
- Unified release: one git tag produces 12 artifacts (2 binaries x 6 platform targets)

#### Documentation

- Added gsqz user guide to `docs/guides/`
- Updated README with workspace overview, documentation links, and expanded tool descriptions
- Added CHANGELOG

### Fixes

#### CI/CD

- Fix macOS-13 runner deprecation in release workflow (#27)
- Fix cross-compilation with vendored OpenSSL + rustls for reqwest (#26)

## [0.1.0]

### Features

#### gcode — AST-Aware Code Search

- Tree-sitter AST parsing for 18 languages across 3 tiers (Python, JS, TS, Go, Rust, Java, C, C++, C#, Ruby, PHP, Swift, Kotlin, Dart, Elixir, JSON, YAML, Markdown)
- SQLite FTS5 full-text search on symbols and file content
- Semantic vector search via Qdrant with GGUF embeddings (macOS Metal GPU)
- Reciprocal Rank Fusion to merge FTS5 + semantic + graph results
- Neo4j dependency graph: callers, usages, imports, blast-radius analysis
- Standalone mode with self-initializing schema and `.gobby/gcode.json` identity
- Gobby mode with `project.json` detection and shared `gobby-hub.db`
- Incremental indexing with SHA-256 content hashing
- `gcode init` with progress bar, auto-indexing, and AI CLI skill installation (#16, #18, #19)
- Confirmation prompt on `gcode invalidate` (#20)
- Graceful degradation when Neo4j/Qdrant/GGUF model unavailable
- Cross-project queries by name or path
- JSON and text output formats

#### gsqz — Output Compression

- YAML-configurable output compressor for LLM token optimization
- 28 built-in compression pipelines (git, cargo, pytest, npm, eslint, ruff, and more)
- 4 composable step types: `filter_lines`, `group_lines`, `truncate`, `dedup`
- 8 grouping modes: `git_status`, `pytest_failures`, `test_failures`, `lint_by_rule`, `by_extension`, `by_directory`, `by_file`, `errors_warnings`
- Layered config: built-in → global (`~/.gobby/gsqz.yaml`) → project (`.gobby/gsqz.yaml`) → CLI override
- Per-section truncation with configurable section markers
- ANSI escape code stripping
- Optional Gobby daemon integration for runtime config and savings reporting
- Claude Code shell wrapper integration
- Always exits code 0 — LLM reads pass/fail from content

#### Platform Support

- macOS (aarch64, x86_64) — with local embeddings via Metal GPU
- Linux (x86_64, aarch64) — without embeddings (embeddings added in 0.3.0)
- Windows (x86_64, aarch64) — without embeddings (embeddings added in 0.3.0)

### Fixes

- Fix standalone config isolation and invalidate cleanup (#12)
- Fix `delete_file_graph` to preserve incoming CALLS edges (#15)
- Add `scoped_identifier` to Rust call query for cross-module calls (#13)
- Fix clippy warnings, remove dead code, feature-gate embeddings (#10)
- Add Gobby hint to empty graph command responses (#25)

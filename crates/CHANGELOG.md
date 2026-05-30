<!-- markdownlint-disable MD024 MD013 -->

# Changelog

All notable changes to gobby-cli are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.7] — gcode

### Fixed

#### gcode

- **Indexed grep unsupported flags** — Unsupported grep/rg flags such as
  `--files-with-matches` now preserve the indexed-search error message while
  still allowing supported options like `-m/--max-count` after positional path
  filters.

## [0.9.6] — gcode

### Changed

#### gcode

- **Indexed grep default output** — `gcode grep` now defaults to text output
  when `--format` is omitted, while explicit `--format json` still returns the
  JSON envelope. Other `gcode` commands keep their JSON default.

### Fixed

#### gcode

- **Indexed grep option ordering** — `gcode grep <pattern> PATH -m N` and
  `gcode grep <pattern> PATH --max-count N` now parse `-m/--max-count`
  correctly after positional path filters instead of treating the flag as an
  unsupported path value.

## [0.9.5] — gcode

### Added

#### gcode

- **Indexed grep** — `gcode grep <pattern> [PATH ...]` now provides exact
  line-oriented search over the indexed `code_content_chunks` corpus. It
  supports `-i`, `-F`, `-C/-A/-B`, `-g/--glob`, and `-m/--max-count`, with text
  output shaped like grep and JSON output that includes match spans, context,
  scan counts, and truncation state.

### Changed

#### gcode

- **Graph sync-file contract** — `gcode graph sync-file` now classifies missing
  indexed projects and files from PostgreSQL before FalkorDB access. Human
  defaults stay strict with typed JSON errors and exit code `2`, while daemon
  and background-worker callers can use `--allow-missing-indexed-file` to turn
  stale missing-file work into a skipped payload.

## [0.4.4] — gobby-hooks

### Added

#### gobby-hooks

- **Tmux pane terminal context** — `ghook` now injects
  `input_data.terminal_context.tmux_pane` for any dispatch path when `TMUX` is
  set and `TMUX_PANE` matches the daemon's `^%\d+$` contract. Missing, empty,
  or invalid pane IDs leave `terminal_context` absent, so the daemon only sees
  pane metadata it can validate and use for tmux window titles.

## [0.9.4] — gcode

### Changed

#### gcode

- **Shared foundation floor** — `gobby-code` now requires `gobby-core 0.2.2`
  so published installs pick up the FalkorDB and Qdrant adapter behavior used
  by this release.
- **FalkorDB client boundary** — graph query execution now routes through
  `gobby-core::falkor::GraphClient`, keeping direct FalkorDB connection and
  result parsing logic inside the shared foundation crate while preserving the
  existing `gcode` graph facade.

### Fixed

#### gcode

- **Graph empty-result fallback** — callers, usages, and blast-radius share the
  same unresolved-symbol empty response path after graph-read availability
  checks, avoiding duplicated fallback behavior across graph commands.

## [0.2.2] — gobby-core

### Added

#### gobby-core

- **FalkorDB graph escape hatch** — `GraphClient::with_sync_graph` exposes the
  underlying synchronous FalkorDB graph to consumers that need operations not
  yet covered by the shared adapter API, while keeping graph selection and
  connection setup centralized.

### Fixed

#### gobby-core

- **Qdrant HTTP classification** — Qdrant server errors now degrade as
  `ServiceState::Unreachable`, while client-side HTTP errors remain typed hard
  failures with response bodies preserved for diagnostics.

## [0.4.3] — gobby-hooks

### Changed

#### gobby-hooks

- **Planned-shutdown Stop documentation** — ghook docs now spell out Stop
  preflight behavior, post-enqueue suppression, marker lookup, and the
  `GOBBY_DAEMON_URL` / `GOBBY_SHUTDOWN_HOOK_ALLOW_SECONDS` controls used during
  intentional daemon stop and restart windows.
- **Stop preflight naming** — the internal planned-shutdown preflight helper now
  uses explicit skip-dispatch naming, matching the behavior that returns
  `{"continue":true}` before stdin reads or enqueue side effects.

## [0.4.2] — gobby-hooks

### Added

#### gobby-hooks

- **Native planned-shutdown Stop handling** — `ghook` now recognizes fresh
  daemon shutdown markers for intentional `stop`/`restart` windows. If a Stop
  hook fires after the daemon is already unreachable, it returns
  `{"continue":true}` without reading stdin or enqueueing a duplicate Stop
  envelope.

### Fixed

#### gobby-hooks

- **Stop daemon-death race cleanup** — when the live Stop POST fails with a
  connection or timeout error during a fresh planned shutdown, `ghook` removes
  the just-enqueued Stop envelope and lets the host CLI continue. HTTP errors,
  non-Stop hooks, stale or invalid markers, and envelope delete failures keep
  the existing fail-closed behavior.

## [0.9.3] — gcode

### Changed

#### gcode

- **Shared foundation floor** — `gobby-code` now requires `gobby-core 0.2.1`
  so published installs pick up the context, PostgreSQL, and Qdrant behavior
  used by this release.

### Fixed

#### gcode

- **Graph read degradation** — callers, usages, imports, and blast-radius
  graph commands now return empty paged results when FalkorDB is unavailable
  instead of failing before a readable response can be produced.
- **Graph payload completeness** — file and symbol graph payloads now include
  their center nodes, and file-target blast-radius queries dedupe merged
  call/import rows before applying the requested limit.
- **Graph report scalability** — graph reports now load aggregate FalkorDB
  summaries instead of materializing every node and edge for production
  snapshots.
- **Index write throughput** — symbol upserts now batch rows in PostgreSQL
  instead of issuing one statement per symbol.
- **Vector sync lifecycle** — code-symbol vector rebuilds now upsert fresh
  points before deleting stale vectors, and clear operations avoid unnecessary
  embedding schema probes.
- **Standalone setup atomicity** — standalone setup wraps reset/create work in
  an explicit PostgreSQL transaction and reports failed creation entries.
- **Short project ids** — short-id rendering now uses Unicode-safe character
  boundaries.
- **Typed graph query literals** — Cypher string literal rendering now escapes
  control characters instead of rejecting otherwise valid strings.

## [0.2.1] — gobby-core

### Changed

#### gobby-core

- **Context API boundary** — `CoreContext` now exposes accessor methods rather
  than public fields, and stores `daemon_url` as a concrete string.
- **PostgreSQL TLS handling** — PostgreSQL connections now honor `sslmode` and
  use native TLS for `prefer` and `require` modes.

### Fixed

#### gobby-core

- **Qdrant degradation** — Qdrant HTTP and transport failures now return
  `ServiceState::Unreachable` from `with_qdrant` instead of surfacing as hard
  command errors.
- **Provisioning tests** — environment-variable mutation in provisioning tests
  now uses a scoped lock/restore guard.

## [0.9.2] — gcode

### Added

#### gcode

- **Project-id graph clear** — `gcode graph clear --project-id <PROJECT_ID>`
  now clears a code graph projection by explicit project id before normal
  project-root resolution. This is the daemon stale-project cleanup path and can
  run from any cwd without `.gobby/project.json`.

### Fixed

#### gcode

- **Deleted-file projection cleanup** — `gcode index` now removes FalkorDB code
  graph nodes/edges and Qdrant code-symbol vectors for deleted files before
  deleting PostgreSQL hub facts. This covers missing explicit
  `--files <deleted-file>` inputs and whole-project stale/orphan cleanup without
  relying on daemon reconciliation.
- **Projection ownership boundary** — code graph clears remain scoped to
  code-index FalkorDB labels, and code vector clears remain scoped to
  `code_symbols_{project_id}`. Memory graph nodes and memory vector collections
  are not targeted by these lifecycle paths.

## [0.9.1] — gcode

### Added

#### gcode

- **Overview graph limit** — `gcode graph overview` now accepts `--limit N`
  to cap the number of files included in the overview graph, matching the
  daemon's graph overview limit contract.

### Fixed

#### gcode

- **File graph read aliases** — `gcode graph file` now keeps node file paths
  and edge metadata file paths under distinct FalkorDB result aliases, fixing
  duplicate-column failures when returning JSON graph payloads.

## [0.9.0] — gcode

### Added

#### gcode

- **Standalone setup reset boundary** — `gcode setup --standalone` now fails
  safely when it detects incompatible existing code-index PostgreSQL state and
  prints guidance to rerun with `--overwrite-code-index` only when a full
  code-index reset is intended.
- **Advanced full code-index overwrite** —
  `gcode setup --standalone --overwrite-code-index` drops/recreates only
  allowlisted gcode code-index PostgreSQL relations and BM25 indexes, clears
  code-index graph nodes in FalkorDB, and deletes Qdrant collections with the
  `code_symbols_` prefix. Gobby project files, config, secrets, tasks,
  sessions, memory, and daemon-owned data stay untouched.
- **Rust graph/vector projection lifecycle** — graph reads, graph reports,
  vector projection sync, and graph/vector lifecycle operations now route
  through the Rust `gobby-code` library boundary for daemon adoption.

### Changed

#### gcode

- **Project-scoped invalidation** — `gcode invalidate` remains the normal
  project reset. PostgreSQL deletes stay filtered to the current project, and
  configured standalone FalkorDB/Qdrant projections are cleaned only for that
  project.
- **Shared foundation dependency** — `gobby-code` now consumes
  `gobby-core 0.2`.

## [0.2.0] — gobby-core

### Added

#### gobby-core

- **Expanded shared foundation** — added reusable context/config contracts,
  attached/standalone setup contracts, PostgreSQL hub helpers, FalkorDB and
  Qdrant adapters, standalone service provisioning helpers, indexing
  primitives, search-fusion primitives, and degradation vocabulary for Rust
  Gobby CLI consumers.

### Changed

#### gobby-core

- **Consumer dependency line** — workspace consumers now target the
  `gobby-core 0.2` minor line.

## [0.8.7] — gcode

### Fixed

#### gcode

- **Project identity resolution** — self-referential
  `parent_project_path` / `parent_project_id` markers now keep the owning
  `.gobby/project.json` ID, while linked worktrees and isolated roots keep
  filesystem-scoped code index IDs.
- **Source `build` package indexing** — root generated `build` / `dist`
  directories stay excluded, while source directories such as
  `src/gobby/build/` are indexed.
- **Duplicate-root pruning** — `gcode prune` now marks stale duplicate project
  entries for an existing root when they differ from that root's resolved
  project ID.

## [0.4.4] — gsqz

### Fixed

#### gsqz

- **Raw passthrough for inspection CLIs** — `gh`, `rg`, and `sed` now join the
  built-in exclusion list so GitHub CLI output, ripgrep search results, and
  exact file slices surface verbatim with no compression header or daemon
  savings report.

## [0.8.6] — gcode

### Added

#### gcode

- **Graph-aware symbol lookup** — `gcode search-symbol --with-graph` keeps
  exact-first ranking and adds FalkorDB graph neighbors when graph config is
  available.

### Changed

#### gcode

- **Search scoring metadata** — hybrid JSON output now uses `score` for the
  final displayed rank score, exposes raw RRF as `rrf_score`, and sorts
  `sources` deterministically.

- **Path-filter fallback visibility** — path globs that cannot be pushed into
  SQL now log a warning and surface a user-facing hint while still enforcing
  exact glob semantics through post-filtering.

### Fixed

#### gcode

- **External call extraction** — scoped Swift imports like
  `import struct Foundation.Date` bind the module root correctly; parameter and
  local variable shadowing prevents false external call targets; Dart textual
  call extraction now tracks raw and triple-quoted multiline strings; C/C++
  macro detection accepts both `#define` and `# define`.

- **clangd cleanup errors** — C/C++ semantic resolution now returns the original
  resolution error first and only closes open clangd files after successful
  resolution.

## [0.1.2] — gloc

### Changed

#### gloc

- **MSRV metadata** — raised the crate `rust-version` to 1.88 to match the
  workspace policy.

## [0.8.5] — gcode

### Changed

#### gcode

- **Positional search paths** — `gcode search`, `gcode search-symbol`,
  `gcode search-text`, and `gcode search-content` now accept one or more
  positional path filters after the query. Bare paths match exact files and
  descendants; glob paths stay verbatim; multiple paths use OR semantics.

- **Broker-first PostgreSQL DSN resolution** — `gcode` now asks the local
  daemon broker for the PostgreSQL hub DSN before consulting any daemonless
  fallback. If the broker is unavailable, explicit fallback sources resolve in
  this order: `GCODE_DATABASE_URL`, `GOBBY_POSTGRES_DSN`,
  `~/.gobby/gcode.yaml` `database_url`, then bootstrap inline `database_url`.

- **Bootstrap `database_url_ref` rejected** — `~/.gobby/bootstrap.yaml`
  `database_url_ref` values are no longer accepted by `gcode`. Daemonless
  setups must provide an inline `database_url` or one of the explicit fallback
  sources above; broker-managed secrets stay behind the daemon broker.

### Fixed

#### gcode

- **Directory path indexing root** — `gcode index <path>` now indexes from the
  resolved project root when `<path>` is inside the current project, keeping
  relative file paths stable instead of treating the passed directory as a new
  root.

### Removed

#### gcode

- **`--path` search filters** — breaking CLI cleanup: `gcode search`,
  `gcode search-symbol`, `gcode search-text`, and `gcode search-content` no
  longer accept `--path <glob>`. Pass paths and globs positionally after the
  query instead.


## [0.8.4] — gcode

### Changed

#### gcode

- **FalkorDB graph backend transition** — `gcode` now reads graph service
  settings from the Gobby 0.4.x FalkorDB config path (`databases.falkordb.*`)
  and uses FalkorDB for graph reads such as `callers`, `usages`, `imports`,
  `blast-radius`, and graph-boosted search. `gcode 0.8.4+` requires
  `gobby 0.4.0+`; running new `gcode` against an older daemon without
  `databases.falkordb.*` config silently degrades to "graph unavailable".
  Upgrade the daemon and CLI together.

- **Broker-only PostgreSQL DSN refs** — `database_url_ref:
  keyring:gobby:postgres_database_url` and broker-only generated refs now resolve
  only through the local Gobby daemon broker. `gcode` no longer falls back to
  native OS Keychain/credential-store reads from short-lived processes; broker
  failures return a clear daemon connectivity/auth error. Inline `database_url`
  remains supported for explicit daemonless setups.

### Removed

#### gcode

- **Native keyring runtime path** — removed direct `keyring-core` and
  platform-keyring-store dependencies from `gobby-code`.

## [0.8.3] — gcode

### Changed

#### gcode

- **AI CLI skill target refresh** — `gcode init` now installs the bundled `gcode` skill for every supported project-local AI CLI target: Claude Code, Codex, Droid, Grok, Qwen, Gemini CLI (deprecated compatibility), and Antigravity CLI. Gobby-managed projects keep skipping project-local skill installation because Gobby owns CLI wiring there.
- **Gemini compatibility label** — Gemini CLI skill installation remains available for existing users, but code comments, command output labels, and docs now mark it deprecated.

## [0.8.2] — gcode

### Changed

#### gcode

- **More tolerant daemon-brokered DSN lookup** — `gcode` now gives the local daemon broker 3 seconds to return the PostgreSQL DSN, preventing cold daemon responses around 1 second from falling through to macOS Keychain prompts.
- **Install guidance for stale binaries** — docs now call out older `gcode` binaries on `PATH`, since pre-0.8.1 installs bypass the daemon broker and can still trigger native Keychain authorization dialogs.

## [0.8.1] — gcode

### Changed

#### gcode

- **Daemon-brokered PostgreSQL DSN resolution** — `gcode` now resolves `database_url_ref: keyring:gobby:postgres_database_url` through the local Gobby daemon broker first, using `POST /api/local/runtime/database-url` with the runtime `local_cli_token`. If the daemon path is unavailable, unauthorized, malformed, or returns an empty DSN, `gcode` silently falls back to the native OS keyring. Inline `database_url` bootstrap files keep their existing behavior, and unsupported `database_url_ref` values still fail clearly before any broker lookup.
- **No SQLite-backed keyring dependency** — `gcode` now wires platform keyring stores directly through `keyring-core` instead of depending on the `keyring` meta crate, avoiding the SQLite-backed `db-keystore`/`turso` stack in the release binary.

## [0.8.0] — gcode

### Changed

#### gcode

- **PostgreSQL hub runtime cutover** — `gcode` now uses the migrated Gobby PostgreSQL hub as its source of truth instead of a local SQLite/FTS index. It reads `~/.gobby/bootstrap.yaml`, requires `hub_backend: postgres`, and resolves the hub DSN from `database_url_ref` or inline `database_url`. Normal index/search commands still do not require the Gobby daemon process, but they do require the migrated hub schema and `pg_search` BM25 indexes. (#158)
- **Externally managed schema contract** — `gcode` now validates the Gobby-owned hub tables, `pg_search` extension, and required BM25 indexes at runtime rather than creating or migrating schema. This keeps the CLI non-destructive to daemon-managed PostgreSQL state. (#158)
- **Keyring v4 credential lookup** — `database_url_ref` resolution now uses `keyring` 4 with `keyring-core` store-backed entry construction for OS keyring reads. This bumps the `gobby-code` Rust floor to 1.88, matching the upstream keyring v4 requirement. (#159)
- **Bundled code-index skill refresh** — the packaged `gcode` skill copy now tracks the installed Gobby code-index instructions, including exact symbol lookup and freshness guidance. (#156, #157)

## [0.7.0] — gcode

### Added

#### gcode

- **`gcode search-symbol` command** — Exact-first symbol/name lookup with deterministic ranking. Resolves precise names ahead of fuzzy matches before falling back to FTS5, and accepts the same `--kind`, `--language`, and `--path` filters as `gcode search`. Use it when you already know (most of) the name and want the canonical hit at rank 0 instead of letting RRF rerank it. (#151)
- **`--language` filter on search commands** — `search`, `search-symbol`, `search-text`, and `search-content` accept `--language <lang>` to narrow results to a tree-sitter language (e.g. `rust`, `python`, `css`). Composes with `--kind` and `--path`. (#151)
- **`search-content` covers comments, config, and CSS** — content search now indexes and matches the same comment/config/CSS chunks the indexer already wrote, so doc strings, `*.toml`/`*.yaml`/`*.json` config, and stylesheets are reachable from `gcode search-content`. (#151)
- **Isolated index roots** — `Context::resolve` now distinguishes five project-identity sources, written up as `ProjectIdentitySource` (`ProjectJson`, `GcodeJson`, `IsolatedRoot`, `LinkedWorktree`, `Generated`):
  - **Isolation marker** — when `.gobby/project.json` carries `parent_project_path` and/or `parent_project_id`, the directory gets its own filesystem-derived code-index id (UUID5 of the canonical path, namespace `c0de1de0-…`) and is no longer conflated with the parent project. Reading the marker is via the new `project::read_isolation_marker` helper.
  - **Linked git worktrees** — runs from inside a `git worktree add` directory now resolve to the worktree's own top-level (via `git rev-parse --show-toplevel` + `git worktree list --porcelain` parsing in the new `git` module), and the code-index id is derived from that path rather than from any inherited `.gobby/project.json`. A warning is printed when an inherited id would have been used.
  - **Generated** — directories without any identity file get a deterministic UUID5 from the canonical path; `.gobby/gcode.json` is only written when `gcode init` runs (via `MissingIdentity::Generate`). Other commands fall back to `MissingIdentity::Error` and ask the user to run `gcode init`.
- **Read-time freshness checks** — search, symbol, outline, and graph read commands now verify that on-disk source still matches the index before returning results, and incrementally re-index the affected file(s) transparently when they don't. Backed by the new `freshness` module (`FreshnessScope::Project` for project-wide commands, `FreshnessScope::Files` for file-scoped commands like `outline`, plus `ensure_symbol_fresh` for `gcode symbol`). Disable per-call with the new global `--no-freshness` flag, or via `GCODE_FRESHNESS_INFLIGHT=1` for nested processes (a re-entrancy guard so the indexer doesn't recurse into itself). Not a substitute for `gcode index` on bulk changes — intended to keep individual reads honest. (#153)
- **Project-root walk-up consults git worktree top-level** — `Context::resolve`'s walk-up now prefers `git rev-parse --show-toplevel` (treating linked worktrees as their own top-level) before falling back to generic `.git`/`.hg` markers, so commands invoked deep inside a worktree resolve to the right project root. (#153)

### Changed

#### gcode

- **Project-scoped search and graph commands tightened** — search, symbol, outline, and graph commands now validate that resolved file paths still belong to the current project context before returning results. Stale entries from other projects sharing the same `gobby-hub.db` no longer leak across project boundaries. The new `commands::scope` module owns the path-validation helpers (`normalize_file_arg`, `path_exists_in_current_project`, `indexed_file_exists`, `content_chunks_exist`, `current_indexed_path_is_valid`). (#151)
- **`gcode init` reports identity source** — init output now distinguishes `initialized`, `existing`, `gobby`, `isolated`, and `linked-worktree` cases when announcing the project context, so it's obvious which identity source resolved.

## [0.4.1] — gobby-hooks

### Added

#### gobby-hooks

- **Factory droid hook route** — `ghook --gobby-owned --cli=droid --type=<PascalCaseHook>` now treats Factory's droid CLI as a first-class source. Droid hook stdin is passed through unchanged to the unified daemon endpoint as `{"hook_type": "<type>", "input_data": <stdin>, "source": "droid"}`, so the Gobby-side `DroidAdapter` owns the protocol translation.
- **Droid diagnose support** — `ghook --diagnose --cli=droid --type=SessionStart` now reports `cli_recognized: true` and `source: "droid"` so installers can probe for droid-capable ghook binaries.

### Changed

#### gobby-hooks

- **Droid blocking semantics** — droid daemon responses with `continue:false` now exit 2 with the daemon reason while preserving the response JSON on stdout. Other droid block JSON is forwarded on stdout with exit 0 for droid's hook protocol, and daemon transport failures surface as exit 1 stderr diagnostics.

### Fixed

#### gobby-hooks

- **Stop double-emitting Claude PreToolUse denies** — for `--cli=claude`, ghook now narrows the legacy `stderr+exit(2)` channel to daemon responses that explicitly set top-level `continue:false` with a non-empty `stopReason` (the HARD_STOP shape). All other responses — including PreToolUse denies that arrive via `hookSpecificOutput.permissionDecision:"deny"` — are emitted as JSON on stdout with exit 0, matching the structured-channel contract the Python `ClaudeCodeAdapter` already targets. Previously, ghook synthesized a second deny channel on top of the structured one, causing Claude Code to render every PreToolUse deny twice (once as a permission denial, once as a "hook blocking error"). Codex/Gemini/Qwen/Droid paths are unchanged.

## [0.3.1] — gobby-hooks

### Added

#### gobby-hooks

- **Claude statusline handler** — `ghook --gobby-owned --cli=claude --type=statusline` now handles Claude Code statusline ticks directly in Rust. The handler bypasses the normal enqueue-first hook dispatch path, extracts the same token-usage payload as the legacy Python statusline middleware, posts it to `/api/sessions/statusline`, and preserves downstream statusline stdout bytes without adding a newline.
- **Statusline parity fixtures** — Added golden JSON fixtures under `crates/ghook/tests/fixtures/statusline/` so Claude statusline input-to-daemon-payload parity is machine-checkable and survives removal of the Python reference in a later Gobby-side phase.

### Changed

#### gobby-hooks

- **Statusline failure semantics** — Malformed statusline JSON, missing `session_id`, daemon POST failures, and downstream command failures all exit successfully so Claude's statusline display is not broken by telemetry issues. Daemon POSTs use a short background worker wait, and downstream forwarding has a bounded timeout.

## [0.3.0] — gobby-hooks

### Added

#### gobby-hooks

- **Diagnose schema v2 with install provenance** — `ghook --diagnose` now emits two new fields, `install_method` and `install_source_url`, and stamps the output with `schema_version: 2`. Both fields are sourced from an optional sidecar file, `.ghook-install.json`, written by the installer next to the `ghook` binary. When no sidecar is present (e.g. plain `cargo install gobby-hooks`), both fields are `null` — so consumers can identify which install path produced a given binary in bug reports. The new schema lives at `crates/ghook/schemas/diagnose-output.v2.schema.json`; the v1 schema file is preserved unchanged as a frozen historical schema for tools that pinned to v1. The Gobby installer is the canonical sidecar writer; see `docs/guides/ghook-development-guide.md` for the full contract. (#4)

### Changed

#### CI/CD

- **Release-time tag/version alignment guard** — the `release-ghook` workflow now fails fast if the pushed `ghook-v{X}` tag's version suffix doesn't match the version in `crates/ghook/Cargo.toml`. This closes the drift mode that produced [GobbyAI/gobby-cli#4](https://github.com/GobbyAI/gobby-cli/issues/4), where the public installer's `ghook-v{version}` GitHub-asset lookup could silently miss because the tag, crate version, and release name had diverged. The guard runs before clippy/tests so a misaligned tag never reaches crates.io or the GitHub release. (#4)

### Fixed

#### gobby-hooks

- **Preserve non-stop block JSON** — folded forward from the unreleased 0.2.2 prep: `ghook` no longer collapses non-Stop block responses to a bare `Blocked by hook` message; the original block JSON is preserved for downstream consumers. (#141)

## [0.2.1] — gobby-hooks

### Fixed

#### gobby-hooks

- **Drop phantom ACP session registrations** — `ghook` now short-circuits when `GOBBY_HOOKS_DISABLED=1` is set in the process environment, returning `{}` with exit 0 before any side effect (no enqueue, no POST, no terminal-context capture). Daemon-spawned `gemini --acp` / `qwen --acp` subprocesses inherit the host CLI's SessionStart hook; this env flag lets the daemon mark them so they don't register phantom sessions.
- **`gobby_acp_child` in terminal_context** — `terminal_context.capture()` now includes `gobby_acp_child` (read from `GOBBY_ACP_CHILD`). The daemon's SESSION_START handler uses it as a second line of defense to recognize and drop registrations from ACP subprocesses even if the env short-circuit didn't fire.
- **Surface nested `permissionDecisionReason` in block messages** — `extract_reason` now also checks `hookSpecificOutput.permissionDecisionReason` (and `.reason` inside that object) after the top-level fallback keys. Modern Claude Code PreToolUse deny responses carry the reason inside `hookSpecificOutput`; `is_blocked` already recognized the nested shape, but `extract_reason` didn't — so denies surfaced as the bare "Blocked by hook" fallback instead of the daemon's actual message.

## [0.4.3] — gsqz

### Fixed

#### gsqz

- **Built-in exclusion passthrough** — Gobby-owned CLIs (`gobby`,
  `gobby-cli`, `gcode`, `ghook`, `gloc`, `gsqz`) and `git` are now excluded
  from squeezing. Their output is surfaced raw and skips compression headers
  and daemon savings reports.

## [0.4.2] — gsqz

### Fixed

#### gsqz

- **Floor `savings_pct` at 0%** — when compressed output ends up larger than the original, `CompressionResult::savings_pct()` now returns `0.0` instead of a negative percentage. Prevents negative savings values from being reported to the daemon.

## [0.6.2] — gcode

### Added

#### gcode

- **Graph lifecycle commands** — Added `gcode graph clear` and `gcode graph rebuild` as daemon-backed code-index graph lifecycle commands. Both commands use the current resolved project context, POST to the existing `/api/code-index/graph/{clear,rebuild}` endpoints, and keep existing read-side graph commands (`callers`, `usages`, `imports`, `blast-radius`) unchanged and top-level.

### Changed

#### gcode

- **Strict daemon response contract** — `gcode graph clear` and `gcode graph rebuild` now fail hard when project context cannot be resolved, when no daemon URL is configured or reachable, when the daemon returns a non-2xx status, or when a 2xx response body is not valid JSON. Successful `--format json` output prints the daemon payload directly; `--format text` renders a concise summary from the parsed JSON response.

## [0.6.1] — gcode

### Fixed

#### gcode

- **FTS fallback query sanitization** — Escaped `%`, `_`, and `\` correctly in the LIKE-based fallback search path so literal user queries stay literal. Prevents malformed matches and closes a SQL-injection footgun in symbol resolution and name search.
- **FalkorDB correctness cutover** — Completed import-aware call-target classification for Python, JavaScript, and TypeScript. `gcode index` now distinguishes local symbols, unresolved callees, and external modules when writing call data, which reduces bogus graph edges and improves `callers`, `usages`, `blast-radius`, and graph-boosted search relevance. (#137)

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

## [0.2.0] — gobby-hooks

### Changed

#### gobby-hooks

- **Dispatcher parity** — `ghook` now mirrors the Python `hook_dispatcher.py` contract for live hook requests, per-CLI critical-hook handling, SessionStart response forwarding, and host-visible stdout/stderr/exit behavior. This restores startup session-context injection for supported CLIs while keeping the Rust spool/replay internals. (#126)

### Fixed

#### CI/CD

- **ghook release tags** — The `release-ghook` workflow now triggers on `ghook-v*` tags, matching the documented binary-specific release tag format used by this repo. (#126)

## [0.1.1] — gobby-hooks

### Fixed

#### gobby-hooks

- **Windows build** — `crates/ghook/src/detach.rs` declared `extern "system" { fn FreeConsole() -> i32; }` for the Windows-only `FreeConsole()` call. Edition 2024 requires extern blocks to be marked `unsafe`, so the Windows build target failed under Rust 1.95. Mac/Linux unaffected (their `#[cfg(unix)]` path uses `setsid(2)` instead). 0.1.0 published to crates.io but Windows users could not `cargo install gobby-hooks`. (#124)

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

- **Symbol name resolution for graph commands** — `callers`, `usages`, and `blast-radius` now resolve fuzzy input before querying FalkorDB. Resolution tries exact match → LIKE substring match → FTS5 search (names, signatures, docstrings). When multiple matches are found, the best is used and alternatives are shown. When no match is found, a clear "No symbol matching" message is printed (#80)
- **`line_start` on FalkorDB CodeSymbol nodes** — `write_defines` now stores `line_start` on CodeSymbol nodes. `blast-radius` Cypher returns `affected.line_start AS line` for non-zero line numbers in output (requires re-index to populate) (#80)

### Changed

#### gcode

- **Progress bar redesign** — static-width layout with bar on left, counter and filename on right: `[████░░░░░░░░] 21/42 : path/to/file.rs`. No more jumping as path lengths change (#83)

## [0.5.0]

### Changed

#### gcode

- **Defer external writes to daemon** — when the Gobby daemon is available (`daemon_url` resolved and `graph_synced` column detected), `gcode index` now performs SQLite writes only and skips all FalkorDB/Qdrant operations. Sync flags (`graph_synced`, `vectors_synced`) stay at `0` for the daemon's background worker to process. FTS search works immediately; graph/semantic search follows once the daemon syncs. Standalone mode is unchanged (#78)
- `GraphSyncPending` files are skipped during incremental indexing when daemon is available — the daemon worker handles retries instead of gcode (#78)
- Orphan file cleanup defers FalkorDB/Qdrant deletion to the daemon's `reconcile_stores` when daemon is available (#78)
- Qdrant collection creation skipped when daemon handles external sync (#78)

### Added

#### gcode

- **Import/call relation SQLite storage** — `gcode index` now writes parsed import relations (`code_imports` table) and call relations (`code_calls` table) to SQLite when the tables exist (daemon migration v183). Enables the daemon to rebuild FalkorDB graph edges without re-parsing files. Table detection via PRAGMA means no deployment ordering required (#78)
- `vectors_synced` column support — detected at runtime alongside `graph_synced`, set to `0` on file upsert. Allows independent tracking of Qdrant vector sync status vs FalkorDB graph sync (#78)
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

- Two-pass graph expansion for hybrid search — after FTS+semantic return results, top symbol names are used to batch-query FalkorDB for callers and callees, feeding a 4th RRF source (`graph_expand`) that surfaces structurally related symbols (#62)
- `find_callers_batch` and `find_callees_batch` FalkorDB functions with `IN $names` Cypher for efficient batch graph queries (#62)
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
- `graph_synced` column support — detects column in `gobby-hub.db` at runtime, sets to `0` on file upsert, flips to `1` after successful FalkorDB/Qdrant writes. Incremental index auto-detects unsynced files and retries external writes only (#57)
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
- FalkorDB write functions (`write_defines`, `write_calls`, `write_imports`, `delete_file_graph`) now return `Result<()>` instead of silently discarding errors (#56)
- FalkorDB writes in `index_file` are invoked independently instead of short-circuiting — all three are attempted even if one fails (#60)

### Fixed

#### gcode

- Fix `gcode invalidate` returning 404 from daemon — notify daemon before clearing SQLite so it can still read project stats (#52)
- Fix FalkorDB short-circuit write chain preventing `write_calls`/`write_imports` from running after `write_defines` failure (#60)

## [0.3.3]

### Fixed

#### gcode

- Fix `gcode invalidate` returning 404 from daemon — notify daemon to clean FalkorDB/Qdrant **before** deleting from SQLite, so the daemon can still read project stats from the shared `gobby-hub.db` (#52)

## [0.3.2]

### Added

#### gcode

- `--offset` flag on `search`, `search-text`, `search-content`, `callers`, `usages` for stateless pagination (#43)
- `--full` flag on `index` to force non-incremental reindex, cleaning up stale external indices (#43)
- `PagedResponse` envelope on all paginated JSON commands: `{ project_id, total, offset, limit, results, hint }` (#43, #45)
- Text mode pagination footer: `-- 10 of 47 results (use --offset 10 for more)` (#43)
- Accurate `total` counts via FTS5 COUNT queries for `search-text` and `search-content` (#44)
- FalkorDB server-side COUNT queries (`count_callers`, `count_usages`) for accurate graph pagination totals (#45)
- FalkorDB server-side SKIP/LIMIT for `find_callers` and `find_usages` — no more over-fetch and skip in Rust (#48)
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
- `:CodeSymbol` label added to `find_usages` and `count_usages` target node for consistent FalkorDB query planning (#48)

## [0.3.1]

### Fixed

#### gcode

- Fix stale Qdrant vectors causing "failed to look up symbol" warnings during search — `delete_file_data` now cleans up Qdrant vectors alongside SQLite and FalkorDB when re-indexing files (#38)
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

- `gcode prune` command to detect and remove stale project entries (dead paths, relative paths, sentinel UUIDs) with daemon FalkorDB/Qdrant cleanup notification (#25)

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

- `gcode invalidate` now notifies the Gobby daemon to clean FalkorDB graph nodes and Qdrant vectors via `POST /api/code-index/invalidate` (#11)
- Daemon URL resolved from `~/.gobby/bootstrap.yaml` (`daemon_port` + `bind_host`) instead of hardcoded default (#12)
- Migrate config_store keys from `memory.*` to `databases.falkordb.*` and `databases.qdrant.*` namespace (#15)

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
- Fix `test_config_defaults` to actually test `resolve_falkordb_config` defaults (#6)
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
- FalkorDB dependency graph: callers, usages, imports, blast-radius analysis
- Standalone mode with self-initializing schema and `.gobby/gcode.json` identity
- Gobby mode with `project.json` detection and shared `gobby-hub.db`
- Incremental indexing with SHA-256 content hashing
- `gcode init` with progress bar, auto-indexing, and AI CLI skill installation (#16, #18, #19)
- Confirmation prompt on `gcode invalidate` (#20)
- Graceful degradation when FalkorDB/Qdrant/GGUF model unavailable
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

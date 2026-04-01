<!-- markdownlint-disable MD024 -->


# Changelog

All notable changes to gobby-cli are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

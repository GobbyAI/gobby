<!-- markdownlint-disable MD024 -->


# Changelog

All notable changes to gobby-cli are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- Linux (x86_64, aarch64) — without embeddings
- Windows (x86_64, aarch64) — without embeddings

### Fixes
- Fix standalone config isolation and invalidate cleanup (#12)
- Fix `delete_file_graph` to preserve incoming CALLS edges (#15)
- Add `scoped_identifier` to Rust call query for cross-module calls (#13)
- Fix clippy warnings, remove dead code, feature-gate embeddings (#10)
- Add Gobby hint to empty graph command responses (#25)

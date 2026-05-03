<!-- markdownlint-disable MD033 MD041 -->
<p align="center">
  <img src="logo.png" alt="Gobby" width="160" />
</p>

<h1 align="center">gcode</h1>

<p align="center">
  <strong>AST-aware code search and navigation for AI agents.</strong><br>
  Fast symbol lookup, dependency graphs, and semantic search — all from the CLI.
</p>

<p align="center">
  <a href="https://github.com/GobbyAI/gobby-cli/actions/workflows/ci.yml"><img src="https://github.com/GobbyAI/gobby-cli/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/GobbyAI/gobby-cli/releases/latest"><img src="https://img.shields.io/github/v/release/GobbyAI/gobby-cli" alt="Release"></a>
  <a href="https://github.com/GobbyAI/gobby-cli"><img src="built-with-gobby.svg" alt="Built with Gobby"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"></a>
</p>

---

## The Problem

AI coding agents read entire files to find a single function. A 2000-line module gets dumped into the context window when all the agent needed was a 15-line method. Multiply that across a session and you're burning thousands of tokens on code that isn't relevant.

## The Fix

gcode indexes your codebase using tree-sitter AST parsing and gives agents (and humans) precise, token-efficient access to symbols, search results, and dependency graphs.

```
$ gcode search "handleAuth"
[
  {"name": "handleAuth", "kind": "function", "file_path": "src/auth/middleware.ts",
   "line_start": 42, "signature": "async function handleAuth(req, res, next)", ...}
]
```

One search call instead of reading 50 files. 90%+ token savings.

## How It Works

```
codebase → tree-sitter AST → SQLite index → search / retrieve / navigate
                │                   │
     ┌──────────┼──────────┐        │
     │          │          │        │
  symbols    chunks     files    ┌──┴──┐
  (FTS5)    (FTS5)   (hashes)   │     │
                              Neo4j  Qdrant
                             (calls) (vectors)
```

1. **Index** — Walk files, parse ASTs with tree-sitter, extract symbols and content chunks
2. **Store** — SQLite for symbols + FTS5, Neo4j for call/import graphs, Qdrant for semantic vectors
3. **Search** — Hybrid ranking: FTS5 + optional semantic + optional graph sources → Reciprocal Rank Fusion
4. **Retrieve** — Byte-offset reads for exact symbol source, no file-level bloat

## Installation

### Pre-built binaries

Download from [GitHub Releases](https://github.com/GobbyAI/gobby-cli/releases/latest):

```bash
# macOS (Apple Silicon)
curl -L https://github.com/GobbyAI/gobby-cli/releases/latest/download/gcode-aarch64-apple-darwin.tar.gz | tar xz
sudo mv gcode /usr/local/bin/

# macOS (Intel)
curl -L https://github.com/GobbyAI/gobby-cli/releases/latest/download/gcode-x86_64-apple-darwin.tar.gz | tar xz
sudo mv gcode /usr/local/bin/

# Linux (x86_64)
curl -L https://github.com/GobbyAI/gobby-cli/releases/latest/download/gcode-x86_64-unknown-linux-gnu.tar.gz | tar xz
sudo mv gcode /usr/local/bin/

# Linux (ARM64)
curl -L https://github.com/GobbyAI/gobby-cli/releases/latest/download/gcode-aarch64-unknown-linux-gnu.tar.gz | tar xz
sudo mv gcode /usr/local/bin/

# Windows (x86_64) — PowerShell
Invoke-WebRequest -Uri https://github.com/GobbyAI/gobby-cli/releases/latest/download/gcode-x86_64-pc-windows-msvc.zip -OutFile gcode.zip
Expand-Archive gcode.zip -DestinationPath .
```

### Build from source

```bash
cargo install gobby-code
```

Graph and semantic features are configured at runtime. You do not need Cargo
feature flags to enable Neo4j, Qdrant, or embeddings support.

### With Gobby

gcode is installed automatically as part of the [Gobby](https://github.com/GobbyAI/gobby) platform. If you're using Gobby, you already have it.

## Usage

```bash
# Initialize and index a project (one step)
gcode init

# Search
gcode search "query"                      # Hybrid: FTS + semantic + graph boost
gcode search "query" --kind function      # Filter by symbol kind
gcode search "query" --language rust      # Filter by source language
gcode search "query" --path "src/**/*.rs" # Filter by file path glob
gcode search-symbol "outline"             # Exact-first symbol/command lookup
gcode search-symbol "outline" --kind function --language rust
gcode search-text "query"                 # FTS5 on symbol names/signatures
gcode search-content "query"              # FTS5 on file content, comments, config, CSS

# Symbol retrieval
gcode outline src/auth.ts                 # Hierarchical symbol tree
gcode symbol <id>                         # Source code by symbol ID
gcode symbols <id1> <id2> ...             # Batch retrieve
gcode tree                                # File tree with symbol counts

# Dependency graph reads (requires Neo4j)
gcode callers "handleAuth"                # Who calls this?
gcode usages "handleAuth"                 # Incoming call sites
gcode imports src/auth.ts                 # Import graph for a file
gcode blast-radius "handleAuth" --depth 3 # Transitive impact analysis

# Graph lifecycle (requires Gobby daemon)
gcode graph clear                         # Clear current project's graph projection
gcode graph rebuild                       # Rebuild current project's graph projection

# Project management
gcode status                              # Index stats
gcode projects                            # List all indexed projects
gcode index                               # Re-index (incremental)
gcode invalidate                          # Clear index, force full re-index

# Cross-project queries
gcode search --project myapp "query"      # By project name
gcode search --project /path/to/app "q"   # By path

# Global flags
--format text|json                        # Output format (default: json)
--quiet                                   # Suppress warnings and progress
--no-freshness                            # Skip read-time index/source freshness checks
```

## Standalone vs Gobby

gcode works out of the box with zero dependencies — just `gcode init` and search. But it's designed to unlock its full potential with [Gobby](https://github.com/GobbyAI/gobby).

### Standalone

```
codebase → tree-sitter → SQLite
                          (symbols + FTS5)
```

Full indexing and text search. No external services needed.

### With Gobby

```
codebase → tree-sitter → SQLite        → FTS5 search
                          Neo4j         → call graphs, blast radius, imports
                          Qdrant + embeddings API → semantic vector search
                          Gobby daemon  → auto-indexing, LLM summaries,
                                          config, secrets, sessions, agents
```

Gobby adds graph queries, graph lifecycle orchestration, semantic search, and infrastructure that makes gcode better at its core job — not just more features bolted on.

**Search quality improves.** With Neo4j, `gcode search` blends FTS5 text matching with call-graph relevance. With Qdrant plus a configured embeddings API, conceptual queries like "database connection pooling" can find semantically similar code even when the exact words don't match.

**Config and secrets are managed.** Neo4j URLs, Qdrant API keys, and auth credentials are stored in the shared database and encrypted with Fernet. No env vars to juggle.

**Indexing happens automatically.** The Gobby daemon watches for file changes and re-indexes in the background. Standalone requires manual `gcode index`.

| Capability | Standalone | With Gobby |
|-----------|-----------|-----------|
| AST indexing + FTS5 search | Yes | Yes |
| Graph-boosted search ranking | — | Yes (Neo4j) |
| Semantic vector search | — | Yes (Qdrant + embeddings API) |
| Call graph / blast radius | — | Yes (Neo4j) |
| Import graph | — | Yes (Neo4j) |
| Graph clear / rebuild lifecycle | — | Yes (daemon-backed) |
| Auto-indexing on file change | — | Yes (daemon file watcher) |
| Centralized config + secrets | — | Yes (encrypted, no env vars) |
| Shared index (daemon + CLI) | — | Yes (gobby-hub.db) |
| AI agent orchestration | — | Yes |
| Persistent sessions + memory | — | Yes |
| Task tracking + pipelines | — | Yes |

Get started with Gobby at [github.com/GobbyAI/gobby](https://github.com/GobbyAI/gobby).

## Graceful Degradation

| Service unavailable | Behavior |
|---------------------|----------|
| Neo4j down | Graph commands return `[]`. Search loses graph boost. |
| Qdrant down | Search loses semantic boost. FTS5 + graph still work. |
| Embeddings API unavailable | Semantic embeddings disabled. FTS5 + graph still work. |
| No index yet | Commands error with `Run gcode init to initialize`. |

Read-side graph commands depend on Neo4j. `gcode graph clear` and `gcode graph rebuild`
are separate lifecycle operations routed through the Gobby daemon for the
current resolved project.

## Language Support

gcode parses ASTs using tree-sitter with support for 18 languages:

| Tier | Languages |
|------|-----------|
| **Tier 1** | Python, JavaScript, TypeScript, Go, Rust, Java, C, C++, C#, Ruby, PHP, Swift, Kotlin |
| **Tier 2** | Dart, Elixir |
| **Tier 3** | JSON, YAML, Markdown (content indexing only) |

## Build

`gcode` uses runtime-configured services rather than Cargo feature flags.

```bash
cargo build --release
cargo test --no-default-features
cargo clippy --no-default-features -- -D warnings
```

## Platform Support

| Platform | Architecture | Status |
|----------|-------------|--------|
| macOS | Apple Silicon (aarch64) | Supported |
| macOS | Intel (x86_64) | Supported |
| Linux | x86_64 | Supported |
| Linux | ARM64 (aarch64) | Supported |
| Windows | x86_64 | Supported |
| Windows | ARM64 (aarch64) | Supported |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

## License

[Apache 2.0](LICENSE) — Use it, fork it, build on it.

---

<p align="center">
  <sub>Part of the <a href="https://github.com/GobbyAI/gobby">Gobby</a> suite.</sub>
</p>

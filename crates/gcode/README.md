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
  <a href="https://github.com/GobbyAI/gobby-code/actions/workflows/ci.yml"><img src="https://github.com/GobbyAI/gobby-code/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/GobbyAI/gobby-code/releases/latest"><img src="https://img.shields.io/github/v/release/GobbyAI/gobby-code" alt="Release"></a>
  <a href="https://github.com/GobbyAI/gobby-code"><img src="built-with-gobby.svg" alt="Built with Gobby"></a>
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
3. **Search** — Hybrid ranking: FTS5 + semantic similarity + graph relevance → Reciprocal Rank Fusion
4. **Retrieve** — Byte-offset reads for exact symbol source, no file-level bloat

## Installation

### Pre-built binaries

Download from [GitHub Releases](https://github.com/GobbyAI/gobby-code/releases/latest):

```bash
# macOS (Apple Silicon)
curl -L https://github.com/GobbyAI/gobby-code/releases/latest/download/gcode-aarch64-apple-darwin.tar.gz | tar xz
sudo mv gcode /usr/local/bin/

# macOS (Intel)
curl -L https://github.com/GobbyAI/gobby-code/releases/latest/download/gcode-x86_64-apple-darwin.tar.gz | tar xz
sudo mv gcode /usr/local/bin/

# Linux (x86_64)
curl -L https://github.com/GobbyAI/gobby-code/releases/latest/download/gcode-x86_64-unknown-linux-gnu.tar.gz | tar xz
sudo mv gcode /usr/local/bin/

# Linux (ARM64)
curl -L https://github.com/GobbyAI/gobby-code/releases/latest/download/gcode-aarch64-unknown-linux-gnu.tar.gz | tar xz
sudo mv gcode /usr/local/bin/

# Windows (x86_64) — PowerShell
Invoke-WebRequest -Uri https://github.com/GobbyAI/gobby-code/releases/latest/download/gcode-x86_64-pc-windows-msvc.zip -OutFile gcode.zip
Expand-Archive gcode.zip -DestinationPath .
```

### Build from source

```bash
# With embeddings (requires cmake for llama-cpp-2)
cargo install --git https://github.com/GobbyAI/gobby-code

# Without embeddings (no cmake needed)
cargo install --git https://github.com/GobbyAI/gobby-code --no-default-features
```

### With Gobby

gcode is installed automatically as part of the [Gobby](https://github.com/GobbyAI/gobby) platform. If you're using Gobby, you already have it.

## Usage

```bash
# Initialize and index a project (one step)
gcode init

# Search
gcode search "query"                      # Hybrid: FTS + semantic + graph boost
gcode search "query" --kind function      # Filter by symbol kind
gcode search-text "query"                 # FTS5 on symbol names/signatures
gcode search-content "query"              # FTS5 on file content

# Symbol retrieval
gcode outline src/auth.ts                 # Hierarchical symbol tree
gcode symbol <id>                         # Source code by symbol ID
gcode symbols <id1> <id2> ...             # Batch retrieve
gcode tree                                # File tree with symbol counts

# Dependency graph (requires Neo4j)
gcode callers "handleAuth"                # Who calls this?
gcode usages "handleAuth"                 # All references (calls + imports)
gcode imports src/auth.ts                 # Import graph for a file
gcode blast-radius "handleAuth" --depth 3 # Transitive impact analysis

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
                          Qdrant + GGUF → semantic vector search
                          Gobby daemon  → auto-indexing, LLM summaries,
                                          config, secrets, sessions, agents
```

Gobby adds graph queries, semantic search, and infrastructure that makes gcode better at its core job — not just more features bolted on.

**Search quality improves.** With Neo4j, `gcode search` blends FTS5 text matching with call-graph relevance. Symbols that are heavily referenced rank higher. With Qdrant, conceptual queries like "database connection pooling" find semantically similar code even when the exact words don't match.

**Summaries appear.** `gcode summary <symbol_id>` returns AI-generated explanations of what a symbol does. The Gobby daemon generates these; standalone always returns null.

**Config and secrets are managed.** Neo4j URLs, Qdrant API keys, and auth credentials are stored in the shared database and encrypted with Fernet. No env vars to juggle.

**Indexing happens automatically.** The Gobby daemon watches for file changes and re-indexes in the background. Standalone requires manual `gcode index`.

| Capability | Standalone | With Gobby |
|-----------|-----------|-----------|
| AST indexing + FTS5 search | Yes | Yes |
| Graph-boosted search ranking | — | Yes (Neo4j) |
| Semantic vector search | — | Yes (Qdrant + GGUF) |
| Call graph / blast radius | — | Yes (Neo4j) |
| Import graph | — | Yes (Neo4j) |
| LLM symbol summaries | — | Yes (daemon-generated) |
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
| GGUF model missing | Semantic embeddings disabled. FTS5 + graph still work. |
| No index yet | Commands error with `Run gcode init to initialize`. |

## Language Support

gcode parses ASTs using tree-sitter with support for 18 languages:

| Tier | Languages |
|------|-----------|
| **Tier 1** | Python, JavaScript, TypeScript, Go, Rust, Java, C, C++, C#, Ruby, PHP, Swift, Kotlin |
| **Tier 2** | Dart, Elixir |
| **Tier 3** | JSON, YAML, Markdown (content indexing only) |

## Build Features

The `embeddings` Cargo feature (default: on) enables local GGUF embedding generation via `llama-cpp-2`. Requires cmake to build. macOS builds use Metal GPU acceleration.

```bash
cargo build --release                        # With embeddings
cargo build --release --no-default-features  # Without embeddings (no cmake)
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

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
codebase → tree-sitter AST → PostgreSQL hub → search / retrieve / navigate
                │                   │
     ┌──────────┼──────────┐        │
     │          │          │        │
  symbols    chunks     files    ┌──┴──┐
  (BM25)    (BM25)   (hashes)   │     │
                              FalkorDB Qdrant
                             (calls) (vectors)
```

1. **Index** — Walk files, parse ASTs with tree-sitter, extract symbols and content chunks
2. **Store** — PostgreSQL hub tables for symbols/content, FalkorDB for call/import graphs, Qdrant for semantic vectors
3. **Search** — Hybrid ranking: pg_search BM25 + optional semantic + optional graph sources → Reciprocal Rank Fusion
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
feature flags to enable FalkorDB, Qdrant, or embeddings support. Gobby-managed
projects read FalkorDB settings from `databases.falkordb.*`; daemon-independent
setups can use `GOBBY_FALKORDB_HOST`, `GOBBY_FALKORDB_PORT`, and
`GOBBY_FALKORDB_PASSWORD`.

Runtime indexing/search requires a migrated Gobby PostgreSQL hub. gcode reads
`~/.gobby/bootstrap.yaml`, requires `hub_backend: postgres`, and resolves the
hub DSN from `database_url_ref` or `database_url`. For
`database_url_ref: keyring:gobby:postgres_database_url`, gcode asks the local
Gobby daemon broker for the DSN and fails clearly when the daemon is unavailable.
gcode never reads the native OS keyring directly. The DSN is not written to a
plaintext runtime file. For explicit daemonless setups, use inline
`database_url`.
If macOS keeps asking for Keychain authorization, check `which -a gcode`; stale
binaries from before `0.8.4` can still read Keychain directly.

### With Gobby

gcode is installed automatically as part of the [Gobby](https://github.com/GobbyAI/gobby) platform. If you're using Gobby, you already have it.

## Usage

```bash
# Initialize and index a project (one step)
gcode init

# Search
gcode search "query"                      # Hybrid: BM25 + semantic + graph boost
gcode search "query" --kind function      # Filter by symbol kind
gcode search "query" --language rust      # Filter by source language
gcode search "query" --path "src/**/*.rs" # Filter by file path glob
gcode search-symbol "outline"             # Exact-first symbol/command lookup
gcode search-symbol "outline" --kind function --language rust
gcode search-text "query"                 # BM25 on symbol names/signatures
gcode search-content "query"              # BM25 on file content, comments, config, CSS

# Symbol retrieval
gcode outline src/auth.ts                 # Hierarchical symbol tree
gcode symbol <id>                         # Source code by symbol ID
gcode symbols <id1> <id2> ...             # Batch retrieve
gcode tree                                # File tree with symbol counts

# Dependency graph reads (requires FalkorDB)
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

## AI CLI Skill Installation

For non-Gobby-managed projects, `gcode init` installs the bundled `gcode` skill
for every supported project-local AI CLI target:

| CLI | Project-local files |
|-----|---------------------|
| Claude Code | `.claude-plugin/plugin.json`, `skills/gcode/SKILL.md` |
| Codex | `.codex/skills/gcode/SKILL.md` |
| Droid | `.factory/skills/gcode/SKILL.md` |
| Grok | `.grok/skills/gcode/SKILL.md` |
| Qwen | `.qwen/skills/gcode/SKILL.md` |
| Gemini CLI (deprecated) | `.gemini/skills/gcode/SKILL.md` |
| Antigravity CLI | `.agents/skills/gcode/SKILL.md` |

Gemini CLI remains installed for compatibility with older setups, but it is
deprecated. Gobby-managed projects skip these project-local writes because
Gobby owns CLI wiring.

## Daemon-Independent Runtime

gcode is standalone in the important CLI sense: `gcode index`, `gcode search`,
`gcode status`, and symbol retrieval do not require the Gobby daemon process.
They do require the migrated PostgreSQL hub schema because that hub is the
source of truth for code-index rows.

### With Gobby

```
codebase → tree-sitter → PostgreSQL hub + pg_search BM25
                          FalkorDB              → call graphs, blast radius, imports
                          Qdrant + embeddings   → semantic vector search
                          Gobby daemon          → auto-indexing, graph/vector sync,
                                                  config, secrets, sessions, agents
```

Gobby adds graph queries, graph lifecycle orchestration, semantic search, and infrastructure that makes gcode better at its core job — not just more features bolted on.

**Search quality improves.** With FalkorDB, `gcode search` blends BM25 text matching with call-graph relevance. With Qdrant plus a configured embeddings API, conceptual queries like "database connection pooling" can find semantically similar code even when the exact words don't match.

**Config and secrets are managed.** FalkorDB connection settings, Qdrant API keys, and auth credentials are stored in the shared database and encrypted with Fernet. No env vars to juggle.

**PostgreSQL DSNs stay out of plaintext files.** Isolated gcode runtimes keep
`database_url_ref: daemon:gobby:postgres_database_url`; gcode resolves it
through the daemon broker only.

**Indexing happens automatically.** The Gobby daemon watches for file changes and re-indexes in the background. Without the daemon, run `gcode index` manually.

| Capability | gcode CLI | With Gobby daemon/services |
|-----------|-----------|-----------|
| AST indexing + BM25 search | Yes, via PostgreSQL hub | Yes |
| Graph-boosted search ranking | When FalkorDB is configured | Yes |
| Semantic vector search | When Qdrant + embeddings are configured | Yes |
| Call graph / blast radius | When FalkorDB is configured | Yes |
| Import graph | When FalkorDB is configured | Yes |
| Graph clear / rebuild lifecycle | Requires daemon | Yes |
| Auto-indexing on file change | Manual `gcode index` | Yes (daemon file watcher) |
| Centralized config + secrets | Reads PostgreSQL `config_store` + secrets | Yes |
| Shared index (daemon + CLI) | PostgreSQL hub | PostgreSQL hub |
| AI agent orchestration | — | Yes |
| Persistent sessions + memory | — | Yes |
| Task tracking + pipelines | — | Yes |

Get started with Gobby at [github.com/GobbyAI/gobby](https://github.com/GobbyAI/gobby).

## Graceful Degradation

| Service unavailable | Behavior |
|---------------------|----------|
| FalkorDB down | Graph commands return `[]`. Search loses graph boost. |
| Qdrant down | Search loses semantic boost. BM25 + graph still work. |
| Embeddings API unavailable | Semantic embeddings disabled. BM25 + graph still work. |
| PostgreSQL hub unavailable | Runtime index/search commands fail with a bootstrap or connection error. |
| No index yet | Commands error with `Run gcode init to initialize`. |

Read-side graph commands depend on FalkorDB. `gcode graph clear` and `gcode graph rebuild`
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

# Plan: `gcode` — Fast Rust CLI for Gobby's Code Index

## Context

Subagents (Claude Code Agent tool) can't use MCP — they only have Bash, Read, Write, Grep, Glob. Today they have zero access to gobby's code graph, symbol search, or blast radius analysis.

`gcode` is a standalone Rust CLI that provides the same functionality as the `gobby-code` MCP server, but invocable via Bash. It reads and writes gobby's databases directly — PostgreSQL for symbols/search, Neo4j for the code graph, Qdrant for semantic search. It does NOT need the gobby daemon running.

**What "depends on gobby's infrastructure" means**: gcode uses the same `PostgreSQL hub`, the same Neo4j instance, the same Qdrant instance. It shares the data layer. But it's a standalone process that can index, search, and query the graph independently.

## Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Binary name | `gcode` | Short, `g`-prefixed like `gsqz` |
| Repository | In-repo at `rust/gcode/` | Tightly coupled to gobby's schema, secrets, config — must ship together |
| Daemon dependency | None — fully standalone | The whole point is subagent access without daemon |
| PostgreSQL | Direct read/write (ruPostgreSQL) | Symbols, FTS5 search, file metadata |
| Neo4j | Direct HTTP API (reqwest) | Graph: callers, imports, blast-radius |
| Qdrant | Rust-native direct storage (qdrant-client crate) | Opens `~/.gobby/services/qdrant/` directly, no server needed |
| Embeddings | llama-cpp-2 crate (GGUF, same model as Python) | Bit-identical to Python's llama-cpp-python output, fastembed has nomic normalization/quantization bugs |
| Tree-sitter | Embedded (Rust crate) | Indexing without daemon |
| Config | Read from `~/.gobby/bootstrap.yaml` → DB → `config_store` | Same config as daemon |

## CLI Design

```bash
# Indexing (standalone — tree-sitter in Rust)
gcode index [<path>]                      # full/incremental index
gcode index --files <f1> <f2>             # index specific files
gcode status                              # project stats
gcode invalidate                          # clear index, force re-index

# Search
gcode search <query> [--limit N] [--kind function|class|method]   # hybrid RRF
gcode search-text <query> [--limit N]                             # FTS5 symbols
gcode search-content <query> [--limit N]                          # FTS5 content

# Symbol retrieval
gcode outline <file>                      # hierarchical symbol tree
gcode symbol <id>                         # source by ID (byte-offset read)
gcode symbols <id1> <id2> ...             # batch retrieve
gcode tree                                # file tree with symbol counts

# Graph (Neo4j direct)
gcode callers <symbol_name> [--limit N]   # who calls this?
gcode usages <symbol_name> [--limit N]    # all references
gcode imports <file>                      # import graph
gcode blast-radius <target> [--depth N]   # transitive impact

# Summaries (reads cached, daemon generates)
gcode summary <symbol_id>                 # return cached summary
gcode repo-outline                        # directory-grouped stats

# Global flags
--project <path>     # override project root
--format text|json   # output format (default: json)
--quiet              # suppress warnings
```

## Architecture

```text
gcode CLI (Rust, ~9-15ms startup)
    │
    ├── Config
    │   └── ~/.gobby/bootstrap.yaml → DB path → config_store → service URLs
    │
    ├── PostgreSQL (ruPostgreSQL, read/write)
    │   ├── code_symbols / code_symbols_fts      → search, outline, symbol
    │   ├── code_content_chunks / code_content_fts → search-content
    │   ├── code_indexed_files / _projects        → status, tree, stale detection
    │   └── config_store                          → Neo4j/Qdrant connection info
    │
    ├── Tree-sitter (Rust crate, embedded)
    │   ├── 16 language grammars
    │   ├── Symbol/import/call extraction
    │   ├── Incremental indexing (hash-based stale detection)
    │   └── Writes to PostgreSQL + Neo4j
    │
    ├── Neo4j HTTP API (reqwest)
    │   ├── POST /db/{database}/query/v2    → Cypher queries
    │   ├── Basic Auth (from secrets/config_store)
    │   ├── Reads: callers, usages, imports, blast-radius
    │   └── Writes: CALLS, IMPORTS, DEFINES edges during indexing
    │
    └── Qdrant (Rust-native, direct storage access)
        ├── qdrant-client crate — opens ~/.gobby/services/qdrant/ directly
        ├── llama-cpp-2 crate — loads ~/.gobby/models/nomic-embed-text-v1.5.Q8_0.gguf
        ├── Reads: semantic search boost for hybrid RRF
        └── Writes: symbol embeddings during indexing
```

### LLM Summaries

gcode does NOT generate summaries — it reads cached values from `code_symbols.summary`.

Summary generation is a daemon responsibility (background job). The existing `SymbolSummarizer` infrastructure in Python is wired up but never invoked — this will be fixed as part of this work by adding invocation in the maintenance loop or as a post-index background job.

Summaries improve search quality (indexed in FTS5) and help agents understand symbols without reading full source.

### What gcode Replaces

gcode **replaces** the gobby-code MCP server entirely. It becomes the sole accessor of code index data (PostgreSQL, Neo4j, Qdrant). The Python `code_index/` module can be retired once gcode is stable.

### Project Layout

```text
rust/gcode/
  Cargo.toml
  src/
  main.rs              -- clap CLI, dispatch
  config.rs            -- bootstrap.yaml, config_store, project detection
  secrets.rs           -- Fernet decryption, $secret:NAME resolution, machine_id + salt
  db.rs                -- ruPostgreSQL connection to PostgreSQL hub
  neo4j.rs             -- Neo4j HTTP client (Cypher queries, auth)
  commands/
    index.rs           -- full/incremental indexing
    search.rs          -- search (hybrid RRF), search-text, search-content
    symbols.rs         -- outline, symbol, symbols, tree
    graph.rs           -- callers, usages, imports, blast-radius
    summary.rs         -- summary retrieval (cached), repo-outline
    status.rs          -- project stats, invalidate
  index/
    parser.rs          -- tree-sitter parsing, symbol/import/call extraction
    languages.rs       -- 16 language specs with query strings (port from Python)
    indexer.rs          -- full/incremental orchestrator
    walker.rs          -- git-aware file discovery (ignore crate)
    chunker.rs         -- 100-line overlapping content chunks
    hasher.rs          -- SHA256 content hashing
    security.rs        -- path validation, binary detection
  search/
    fts.rs             -- FTS5 query sanitization + execution
    semantic.rs        -- llama-cpp-2 GGUF embeddings + Qdrant vector search
    graph_boost.rs     -- Neo4j graph boost for search ranking
    rrf.rs             -- Reciprocal Rank Fusion merge (FTS5 + semantic + graph)
  output.rs            -- JSON / text formatters
  models.rs            -- Symbol, IndexedFile, ContentChunk, response types
```

### Data Flow: Indexing

```text
File System
  → walker.rs (ignore crate, .gitignore-aware)
  → hasher.rs (SHA256) → compare with code_indexed_files.content_hash
  → parser.rs (tree-sitter) → Symbol, ImportRelation, CallRelation
  → db.rs (ruPostgreSQL) → write to code_symbols, code_indexed_files, code_content_chunks
  → neo4j.rs → write CALLS, IMPORTS, DEFINES edges
  → semantic.rs (llama-cpp-2 GGUF → Qdrant) → write symbol embeddings
```

### Data Flow: Search

```text
Query string
  → fts.rs (sanitize, FTS5 MATCH on code_symbols_fts) → ranked results
  → semantic.rs (llama-cpp-2 query embedding → Qdrant vector search) → ranked results
  → graph_boost.rs (Neo4j: find related symbols) → boost set
  → rrf.rs (merge FTS5 + semantic + graph boost via RRF k=60) → final ranked results
  → output.rs → JSON or text to stdout
```

### Config + Secret Resolution

gcode replicates the daemon's config chain, including secret decryption:

1. Read `~/.gobby/bootstrap.yaml` → `database_url` (default: `~/.gobby/PostgreSQL hub`)
2. Open PostgreSQL DB (read-only for config)
3. Read `config_store` table → persistence settings:
   - `memory.neo4j_url`, `memory.neo4j_auth`, `memory.neo4j_database`
   - `memory.qdrant_url`, `memory.qdrant_api_key`
4. Resolve `$secret:NAME` patterns in config values:
   - Read `~/.gobby/machine_id` (plain text)
   - Read `~/.gobby/.secret_salt` (16 raw bytes)
   - PBKDF2-HMAC-SHA256 (600,000 iterations, 32-byte key) → base64url → Fernet key
   - Decrypt `encrypted_value` from `secrets` table in `PostgreSQL hub`
5. Resolve `${VAR}` patterns: SecretStore first, then env vars
6. Env var overrides: `GOBBY_NEO4J_URL`, etc.

**Rust crates for secret resolution**: `fernet`, `pbkdf2`, `sha2`, `base64`

Source: `src/gobby/storage/secrets.py`, `src/gobby/utils/machine_id.py`, `src/gobby/config/app.py`

### Project Detection

1. `--project <path>` CLI flag
2. Walk up from cwd for `.gobby/project.json` → read `project_id`
3. Use cwd, look up project by `root_path` in `code_indexed_projects`

### Graceful Degradation

| Service | Down | Behavior |
|---------|------|----------|
| Neo4j | Yes | Graph commands return `[]` with warning. Search loses graph boost. |
| Qdrant storage | Yes | Search loses semantic boost. FTS5 + graph still works. |
| GGUF model | Not downloaded | Search loses semantic boost. FTS5 + graph still works. Warning printed. |
| PostgreSQL | No index | `gcode search` returns nothing. `gcode index` creates it. |

### UUID5 Parity (Critical)

Symbol IDs must match Python. Namespace: `c0de1de0-0000-4000-8000-000000000000`, key: `{project_id}:{file_path}:{name}:{kind}:{byte_start}`.

Source: `src/gobby/code_index/models.py:12,49-52`

### PostgreSQL Concurrency

gcode shares `PostgreSQL hub` with the daemon. Both may read/write concurrently.

- Enable WAL mode (already set by daemon)
- Use `SQLITE_OPEN_READ_WRITE` for indexing, `SQLITE_OPEN_READ_ONLY` for queries
- Short write transactions during indexing
- `busy_timeout(5000)` for lock contention

## Neo4j Cypher Queries

Ported from `src/gobby/code_index/graph.py`:

```cypher
-- find_callers
MATCH (caller:CodeSymbol)-[:CALLS]->(target:CodeSymbol {name: $name})
WHERE target.project_id = $project_id
RETURN caller.id, caller.name, caller.file, caller.line LIMIT $limit

-- find_usages
MATCH (n)-[r:CALLS|IMPORTS]->(target {name: $name})
WHERE target.project_id = $project_id
RETURN n.id, n.name, n.file, n.line, type(r) as relation LIMIT $limit

-- get_imports
MATCH (f:CodeFile {path: $file_path})-[:IMPORTS]->(m:CodeModule)
WHERE f.project_id = $project_id
RETURN m.name, m.id

-- blast_radius
MATCH path = (affected)-[:CALLS|IMPORTS*1..$depth]->(target {name: $name})
WHERE target.project_id = $project_id
RETURN affected.id, affected.name, affected.file, length(path) as distance

-- write edges (during indexing)
MERGE (s:CodeSymbol {id: $source_id})
MERGE (t:CodeSymbol {id: $target_id})
MERGE (s)-[:CALLS {file: $file, line: $line}]->(t)
```

## Rust Dependencies

```toml
[dependencies]
# CLI
clap = { version = "4", features = ["derive"] }

# Serialization
serde = { version = "1", features = ["derive"] }
serde_json = "1"
serde_yaml = "0.9"

# Database
ruPostgreSQL = { version = "0.32", features = ["bundled"] }

# HTTP (Neo4j, Qdrant, daemon)
reqwest = { version = "0.12", features = ["json", "blocking"] }
base64 = "0.22"

# Tree-sitter
tree-sitter = "0.24"
tree-sitter-python = "0.23"
tree-sitter-javascript = "0.23"
tree-sitter-typescript = "0.23"
tree-sitter-go = "0.23"
tree-sitter-rust = "0.23"
tree-sitter-java = "0.23"
tree-sitter-c = "0.23"
tree-sitter-cpp = "0.23"
# + php, ruby, json, yaml, markdown, dart, csharp, elixir

# Qdrant + Embeddings
qdrant-client = "1"       # Direct storage access (Qdrant is Rust-native)
llama-cpp-2 = { version = "0.1", features = ["metal"] }  # GGUF embeddings, same model as Python

# Utilities
sha2 = "0.10"
uuid = { version = "1", features = ["v5"] }
ignore = "0.4"

# Secret resolution (Fernet decryption of gobby secrets)
fernet = "0.2"
pbkdf2 = "0.12"
# base64 already listed above
```

## Installation

New `_install_gcode()` in `src/gobby/cli/install_setup.py`:

1. **Pre-built from GitHub Release** — `GobbyAI/gobby/releases` includes `gcode-{target}.tar.gz` artifacts (built by CI from `rust/gcode/`)
2. **Build from source** (fallback) — `cargo build --release --manifest-path rust/gcode/Cargo.toml` if Rust toolchain available
3. **cargo install from path** (last resort) — `cargo install --path rust/gcode/`

Binary installed to `~/.gobby/bin/gcode`. Version stamp at `~/.gobby/bin/.gcode-version`. PATH management same as gsqz.

## Gobby-Side Changes

1. **Installation**: `_install_gcode()` in `install_setup.py` (builds from `rust/gcode/` or downloads release)
2. **Subagent context**: Inform spawned agents that `gcode` is available
3. **Re-indexing hooks**: AFTER_TOOL hook shells out to `gcode index --files <changed>` instead of calling Python CodeIndexTrigger. Git post-commit hook calls gcode directly instead of daemon HTTP API.
4. **Retire Python code_index**: Once stable, remove `src/gobby/code_index/` and `gobby-code` MCP server (or gate behind feature flag during transition)
5. **No schema changes**: gcode reads/writes the existing tables

### Re-indexing Flow (with gcode)

```text
File edit (tool) → AFTER_TOOL hook → shell out: gcode index --files <path>
Git commit       → post-commit hook → shell out: gcode index --files <changed_files>
```

No daemon in the loop for indexing. gcode handles it directly.

## Implementation Sprints

### Sprint 1: Core — Parse + Store + Search
- Create `rust/gcode/` directory in gobby repo with Cargo project
- `config.rs` — bootstrap.yaml, config_store, project detection
- `secrets.rs` — Fernet decryption, $secret:NAME resolution
- `db.rs` — ruPostgreSQL connection, schema awareness
- `models.rs` — Symbol, IndexedFile, ContentChunk with UUID5 parity
- `index/languages.rs` — port 16 language specs from Python
- `index/parser.rs` — tree-sitter parsing
- `index/walker.rs` — git-aware file discovery
- `index/indexer.rs` — full/incremental indexing (PostgreSQL writes)
- `search/fts.rs` — FTS5 search
- `commands/` — index, search-text, search-content, outline, symbol, status
- `output.rs` — JSON + text formatters

### Sprint 2: Qdrant + Neo4j + Hybrid Search
- `search/semantic.rs` — llama-cpp-2 GGUF embeddings + Qdrant vector storage
  - Load `~/.gobby/models/nomic-embed-text-v1.5.Q8_0.gguf`
  - Match Python: `embedding=true, n_ctx=2048, n_gpu_layers=-1` (metal feature)
  - Task prefixes: `"search_document: "` for indexing, `"search_query: "` for queries
  - 768-dim output, mean pooling (llama.cpp default), NOT pre-normalized
  - Thread safety: `Mutex<LlamaContext>` (llama.cpp is not thread-safe)
  - Graceful fallback to FTS5 when GGUF not downloaded
- Embedding writes during indexing (symbol text → vector → Qdrant)
- `neo4j.rs` — HTTP client, auth, Cypher queries
- Graph writes during indexing (CALLS, IMPORTS, DEFINES)
- `search/graph_boost.rs` + `search/rrf.rs` — full 3-source hybrid RRF
- `commands/graph.rs` — callers, usages, imports, blast-radius
- `commands/search.rs` — full hybrid RRF search

### Sprint 3: Release + Integration
- CI/CD: GitHub Actions build matrix (5 targets), build from `rust/gcode/`
- Python: `_install_gcode()` in `install_setup.py` (build from source or download)
- Python: update AFTER_TOOL hook + git post-commit hook to call `gcode index --files`
- Python: subagent context injection (PATH, prompt hint)
- Python: gate `gobby-code` MCP behind feature flag, default to gcode
- Python: wire up `SymbolSummarizer` — add invocation in maintenance loop for unsummarized symbols
- E2E: subagent uses gcode via Bash
- Benchmark startup time, indexing speed, and query latency

## Follow-Up Work (Out of Scope for Initial Implementation)

### Embedding Parity Verification
After gcode Sprint 2, verify cross-runtime embedding parity: generate same text embedding from Python (llama-cpp-python) and Rust (llama-cpp-2), confirm cosine similarity > 0.999. Both use the same GGUF file so output should be bit-identical.

## Verification

1. **UUID parity**: Rust `Symbol::make_id()` matches Python output for same inputs
2. **Parse parity**: Same files → same symbols (name, kind, byte_start, byte_end)
3. **FTS5 parity**: Same query → same results on same DB
4. **Graph parity**: `gcode callers "X"` matches gobby-code MCP result
5. **Concurrent safety**: Daemon + gcode writing simultaneously doesn't corrupt
6. **Startup time**: Benchmark cold start
7. **Degradation**: Neo4j down → search still works, graph returns `[]`
8. **Subagent E2E**: Spawn Claude subagent, it uses gcode via Bash
9. **Embedding parity**: Same text → cosine similarity > 0.999 between Python and Rust embeddings

## Critical Source Files

| File | Role |
|------|------|
| `src/gobby/code_index/parser.py` | Tree-sitter parsing to replicate in Rust |
| `src/gobby/code_index/languages.py` | 16 language specs to port verbatim |
| `src/gobby/code_index/models.py:12,49-52` | UUID5 namespace + key format |
| `src/gobby/code_index/storage.py` | PostgreSQL schema + FTS5 queries |
| `src/gobby/code_index/searcher.py` | RRF hybrid search algorithm |
| `src/gobby/code_index/graph.py` | Cypher queries for Neo4j |
| `src/gobby/code_index/indexer.py` | Incremental indexing logic |
| `src/gobby/code_index/chunker.py` | Content chunking algorithm |
| `src/gobby/memory/neo4j_client.py` | Neo4j HTTP API pattern |
| `src/gobby/config/persistence.py` | Neo4j/Qdrant config fields |
| `src/gobby/config/bootstrap.py` | Bootstrap config format |
| `src/gobby/search/local_embeddings.py` | llama-cpp-python embedding params to replicate |
| `src/gobby/search/embeddings.py` | Embedding routing layer (prefix behavior) |
| `src/gobby/cli/install_setup.py` (`_install_gsqz()`) | gsqz install template |

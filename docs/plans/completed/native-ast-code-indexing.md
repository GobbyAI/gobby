# Plan: Native AST-Based Code Indexing

## Context

Gobby's agents currently explore code via file-level reads (`Read`, `Grep`, `Glob`), which wastes context window tokens on large codebases. We will demonstrate that tree-sitter AST parsing with symbol-level retrieval can achieve 98-99% token savings. Rather than adding jcodemunch as a dependency, we're building native AST indexing as a `gobby-code` internal MCP registry — leveraging Gobby's existing storage trifecta (SQLite for structured data, Qdrant for embeddings, Neo4j for relationships).

**Indexing trigger:** Manual via MCP tool calls (`index_folder` / `index_repo`), with potential for auto-indexing later.

**Language support:** All 13 languages from day one (Python, JS, TS, Go, Rust, Java, PHP, Dart, C#, C, C++, Elixir, Ruby).

**Storage:** SQLite for symbol metadata + Qdrant for symbol embeddings + Neo4j for call/import graphs.

## Implementation

### Step 1: Add tree-sitter dependencies

**File:** `pyproject.toml`

Add to dependencies:

```
"tree-sitter>=0.24.0",
"tree-sitter-python>=0.23.0",
"tree-sitter-javascript>=0.23.0",
"tree-sitter-typescript>=0.23.0",
"tree-sitter-go>=0.23.0",
"tree-sitter-rust>=0.23.0",
"tree-sitter-java>=0.23.0",
"tree-sitter-php>=0.23.0",
"tree-sitter-c>=0.23.0",
"tree-sitter-cpp>=0.23.0",
"tree-sitter-c-sharp>=0.23.0",
"tree-sitter-ruby>=0.23.0",
"tree-sitter-elixir>=0.3.0",
```

Note: Dart may need `tree-sitter-dart` or a community grammar. Verify availability.

Run `uv sync` after.

### Step 2: Create the code indexing module

**Directory:** `src/gobby/code_index/` (new module)

#### `src/gobby/code_index/__init__.py`

Exports: `CodeIndexer`, `Symbol`, `LanguageSpec`

#### `src/gobby/code_index/models.py` (~80 lines)

Data models:

```python
@dataclass
class Symbol:
    id: str              # "{file_path}::{qualified_name}#{kind}" (jcodemunch format)
    file_path: str
    name: str
    qualified_name: str
    kind: str            # function, class, method, constant, type, import
    language: str
    signature: str       # function signature / class declaration line
    docstring: str | None
    line: int
    end_line: int
    byte_offset: int
    byte_length: int
    parent_id: str | None  # for methods inside classes
    content_hash: str    # SHA-256 of symbol source for drift detection

@dataclass
class FileIndex:
    file_path: str
    language: str
    file_hash: str       # for incremental re-indexing
    symbol_count: int
    indexed_at: str
```

#### `src/gobby/code_index/languages.py` (~200 lines)

Language registry inspired by jcodemunch's `LanguageSpec` pattern:

- Map file extensions → language name → tree-sitter grammar
- For each language, define which AST node types to extract (function_definition, class_definition, method_definition, etc.)
- Support all 13 languages
- Lazy-load grammars (only import tree-sitter-python when parsing .py files)

#### `src/gobby/code_index/parser.py` (~250 lines)

Core AST parser:

- `parse_file(path: str) -> list[Symbol]` — parse a single file into symbols
- Uses tree-sitter to walk AST, extract nodes matching the language spec
- Computes byte offsets, signatures, docstrings
- Generates stable symbol IDs in jcodemunch format
- Handles nested symbols (methods inside classes → parent_id linkage)

#### `src/gobby/code_index/indexer.py` (~300 lines)

High-level indexer orchestrating parse + store:

- `index_folder(path: str, project_id: str) -> IndexResult` — walk directory, filter files, parse each, store symbols
- `index_repo(owner: str, repo: str, project_id: str) -> IndexResult` — clone/fetch from GitHub, then index_folder
- File filtering: extension whitelist, skip patterns (node_modules, .git, vendor, build, dist), .gitignore respect, secret detection, binary detection, 500KB per-file limit
- Incremental indexing: compare file hashes to skip unchanged files
- Security: path traversal prevention, symlink protection (reuse patterns from `src/gobby/code_index/security.py`)

#### `src/gobby/code_index/security.py` (~60 lines)

Security checks (borrowed from jcodemunch's approach):

- Path traversal prevention (resolve + check prefix)
- Symlink escape detection
- Secret file exclusion (.env, *.pem, *.key, credentials.*)
- Binary file detection (extension + null-byte check)

#### `src/gobby/code_index/retrieval.py` (~100 lines)

O(1) symbol retrieval:

- `get_symbol_source(symbol: Symbol, file_path: str) -> str` — read exact bytes using byte_offset + byte_length
- `get_symbols_batch(symbol_ids: list[str]) -> list[tuple[str, str]]` — batch retrieval
- Context lines support (N lines before/after the symbol)
- Content hash verification for drift detection

### Step 3: Database storage layer

**File:** `src/gobby/storage/code_index.py` (~200 lines)

`CodeIndexStorage` class with CRUD:

- `store_symbols(project_id, file_path, symbols)` — upsert symbols for a file
- `get_symbol(symbol_id)` → Symbol
- `get_symbols(symbol_ids)` → list[Symbol]
- `get_file_symbols(project_id, file_path)` → list[Symbol]
- `search_symbols(query, kind, language, file_pattern)` → list[Symbol] with weighted scoring (name match +20, docstring/keyword +3)
- `get_file_tree(project_id)` → nested dict of files with symbol counts
- `get_file_outline(project_id, file_path)` → hierarchical symbol tree
- `get_repo_outline(project_id)` → summary stats
- `store_file_hash(project_id, file_path, hash)` / `get_file_hash(...)` — for incremental indexing
- `invalidate(project_id)` — clear all symbols for a project

**File:** `src/gobby/storage/migrations.py`

Add migration (next version after current baseline) creating tables:

```sql
CREATE TABLE code_symbols (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    language TEXT NOT NULL,
    signature TEXT,
    docstring TEXT,
    line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    byte_length INTEGER NOT NULL,
    parent_id TEXT,
    content_hash TEXT NOT NULL,
    indexed_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_cs_project ON code_symbols(project_id);
CREATE INDEX idx_cs_name ON code_symbols(name);
CREATE INDEX idx_cs_kind ON code_symbols(kind);
CREATE INDEX idx_cs_file ON code_symbols(project_id, file_path);
CREATE INDEX idx_cs_qualified ON code_symbols(qualified_name);

CREATE TABLE code_file_hashes (
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    indexed_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (project_id, file_path)
);

CREATE TABLE code_index_meta (
    project_id TEXT PRIMARY KEY,
    total_symbols INTEGER DEFAULT 0,
    total_files INTEGER DEFAULT 0,
    languages TEXT,  -- JSON array
    git_commit TEXT,
    indexed_at TEXT DEFAULT (datetime('now'))
);
```

### Step 4: MCP tool registry (`gobby-code`)

**File:** `src/gobby/mcp_proxy/tools/code_index.py` (~250 lines)

Create `create_code_index_registry()` factory exposing 11 tools matching jcodemunch's API:

| Tool | Description |
|------|-------------|
| `index_folder` | Index a local directory |
| `index_repo` | Index a GitHub repository |
| `list_repos` | List indexed projects with stats |
| `get_file_tree` | File structure with symbol counts |
| `get_file_outline` | Symbol hierarchy for a file |
| `get_symbol` | Retrieve full source for one symbol |
| `get_symbols` | Batch-retrieve multiple symbols |
| `search_symbols` | Find symbols by name/kind/language/pattern |
| `search_text` | Full-text search across indexed files |
| `get_repo_outline` | High-level project summary |
| `invalidate_cache` | Clear index for a project |

Every response includes a `_meta` object with token savings metrics (estimated tokens saved = `(raw_file_bytes - response_bytes) / 4`).

**File:** `src/gobby/mcp_proxy/registries.py`

Add `code_index_storage` parameter to `setup_internal_registries()`. Conditionally create and register the code index registry.

### Step 5: Wire into the daemon

**File:** `src/gobby/runner.py`

Initialize `CodeIndexer` and `CodeIndexStorage` in `GobbyRunner`. Pass to service container.

**File:** `src/gobby/servers/http.py`

Thread `code_index_storage` through to `setup_internal_registries()`.

### Step 6: Progressive discovery integration

**File:** `src/gobby/mcp_proxy/instructions.py`

Add `gobby-code` to pre-seeded server list with tool descriptions so agents know about symbol-level retrieval without calling `list_mcp_servers()` first.

### Step 7: Token savings tracking

**File:** `src/gobby/code_index/metrics.py` (~50 lines)

Simple tracker that accumulates token savings per session and persists to `code_index_meta` table. Each tool response includes `tokens_saved` (this call) and `total_tokens_saved` (cumulative).

### Step 8: Tests

**Directory:** `tests/code_index/` (new)

- `test_parser.py` — parse Python/JS/TS files, verify symbol extraction
- `test_indexer.py` — index a small fixture directory, verify incremental re-indexing
- `test_retrieval.py` — O(1) byte-offset retrieval, content hash verification
- `test_security.py` — path traversal, symlink, secret detection
- `test_languages.py` — verify all 13 language specs extract correct node types

**File:** `tests/mcp_proxy/tools/test_code_index.py`

- Test the MCP tool registry end-to-end (index → search → retrieve)

**File:** `tests/storage/test_code_index.py`

- CRUD operations, search scoring, file hash tracking

## Critical Files to Modify (existing)

| File | Change |
|------|--------|
| `pyproject.toml` | Add tree-sitter dependencies |
| `src/gobby/storage/migrations.py` | Add code_symbols, code_file_hashes, code_index_meta tables |
| `src/gobby/mcp_proxy/registries.py` | Register gobby-code registry |
| `src/gobby/mcp_proxy/instructions.py` | Pre-seed gobby-code in discovery |
| `src/gobby/runner.py` | Initialize CodeIndexer |
| `src/gobby/servers/http.py` | Thread code indexer to registries |

## New Files

| File | Purpose | Est. Lines |
|------|---------|-----------|
| `src/gobby/code_index/__init__.py` | Module exports | 10 |
| `src/gobby/code_index/models.py` | Symbol, FileIndex dataclasses | 80 |
| `src/gobby/code_index/languages.py` | Language registry (13 languages) | 200 |
| `src/gobby/code_index/parser.py` | Tree-sitter AST parser | 250 |
| `src/gobby/code_index/indexer.py` | Directory walker + orchestrator | 300 |
| `src/gobby/code_index/security.py` | Path/symlink/secret/binary checks | 60 |
| `src/gobby/code_index/retrieval.py` | O(1) byte-offset symbol retrieval | 100 |
| `src/gobby/code_index/metrics.py` | Token savings tracking | 50 |
| `src/gobby/storage/code_index.py` | SQLite CRUD for symbols | 200 |
| `src/gobby/mcp_proxy/tools/code_index.py` | gobby-code MCP registry (11 tools) | 250 |
| `tests/code_index/test_parser.py` | Parser tests | 150 |
| `tests/code_index/test_indexer.py` | Indexer tests | 100 |
| `tests/code_index/test_retrieval.py` | Retrieval tests | 80 |
| `tests/code_index/test_security.py` | Security tests | 60 |
| `tests/code_index/test_languages.py` | Language support tests | 200 |
| `tests/mcp_proxy/tools/test_code_index.py` | MCP tool e2e tests | 150 |
| `tests/storage/test_code_index.py` | Storage CRUD tests | 100 |

**Total: ~2,340 lines of new code + tests**

## What We're NOT Building (v1)

- Qdrant integration for symbol embeddings (add in v2 for semantic code search)
- Neo4j integration for call/import graphs (add in v2 for "find callers of X")
- Auto-indexing on session start (add after manual indexing proves out)
- AI-generated symbol summaries (jcodemunch uses Haiku — we can add this later)
- GitHub API integration for `index_repo` (start with local `index_folder` only, `index_repo` can clone then index)

## Verification

1. `uv sync` — tree-sitter dependencies install cleanly
2. `uv run pytest tests/code_index/ -v` — all parser/indexer/retrieval/security tests pass
3. `uv run pytest tests/storage/test_code_index.py -v` — storage CRUD tests pass
4. `uv run pytest tests/mcp_proxy/tools/test_code_index.py -v` — MCP tool tests pass
5. Manual: start daemon, call `index_folder` on a Python project, then `search_symbols` and `get_symbol` through the MCP proxy
6. `uv run ruff check src/gobby/code_index/ src/gobby/storage/code_index.py src/gobby/mcp_proxy/tools/code_index.py`
7. `uv run mypy src/gobby/code_index/`
8. Verify no regressions: `uv run pytest tests/mcp_proxy/ tests/storage/ -v --ignore=tests/storage/test_code_index.py` (existing tests still pass)

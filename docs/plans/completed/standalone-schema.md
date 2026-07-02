# Standalone Schema: gcode Self-Initializing Database

## Context

gcode currently cannot run without gobby having set up the database first. It has zero `CREATE TABLE` logic — writes silently fail, reads return empty when tables are missing. This plan adds self-initializing schema creation so gcode works fully standalone (FTS5 search, no external deps) while seamlessly integrating when gobby is installed (shared `~/.gobby/gobby-hub.db`).

**Value ladder:** gcode standalone = fast FTS search. With gobby = adds Qdrant semantic search, FalkorDB graph, AI summaries.

## Implementation

### 1. Add `src/schema.rs` (new file)

New module with a single public function `ensure_schema(conn: &Connection) -> anyhow::Result<()>` that runs all CREATE TABLE/INDEX/TRIGGER statements idempotently.

**Tables** (exact match to gobby's Python BASELINE_SCHEMA at `src/gobby/storage/baseline_schema.sql`):

```sql
CREATE TABLE IF NOT EXISTS code_symbols (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    language TEXT NOT NULL,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    signature TEXT,
    docstring TEXT,
    parent_symbol_id TEXT,
    content_hash TEXT NOT NULL,
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS code_indexed_files (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    language TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    symbol_count INTEGER NOT NULL DEFAULT 0,
    byte_size INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, file_path)
);

CREATE TABLE IF NOT EXISTS code_content_chunks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    content TEXT NOT NULL,
    language TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, file_path, chunk_index)
);

CREATE TABLE IF NOT EXISTS code_indexed_projects (
    id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    total_files INTEGER NOT NULL DEFAULT 0,
    total_symbols INTEGER NOT NULL DEFAULT 0,
    last_indexed_at TEXT,
    index_duration_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS savings_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    project_id TEXT,
    category TEXT NOT NULL,
    original_tokens INTEGER NOT NULL,
    actual_tokens INTEGER NOT NULL,
    tokens_saved INTEGER NOT NULL,
    cost_saved_usd REAL,
    model TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

**FTS5 virtual tables:**
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS code_symbols_fts USING fts5(
    name, qualified_name, signature, docstring, summary,
    content='code_symbols', content_rowid='rowid'
);

CREATE VIRTUAL TABLE IF NOT EXISTS code_content_fts USING fts5(
    content, file_path, language,
    content='code_content_chunks', content_rowid='rowid'
);
```

**FTS triggers** (6 total — INSERT/DELETE/UPDATE for each FTS table):
```sql
-- code_symbols_fts
CREATE TRIGGER IF NOT EXISTS code_symbols_ai AFTER INSERT ON code_symbols BEGIN
    INSERT INTO code_symbols_fts(rowid, name, qualified_name, signature, docstring, summary)
    VALUES (new.rowid, new.name, new.qualified_name, new.signature, new.docstring, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS code_symbols_ad AFTER DELETE ON code_symbols BEGIN
    INSERT INTO code_symbols_fts(code_symbols_fts, rowid, name, qualified_name, signature, docstring, summary)
    VALUES ('delete', old.rowid, old.name, old.qualified_name, old.signature, old.docstring, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS code_symbols_au AFTER UPDATE ON code_symbols BEGIN
    INSERT INTO code_symbols_fts(code_symbols_fts, rowid, name, qualified_name, signature, docstring, summary)
    VALUES ('delete', old.rowid, old.name, old.qualified_name, old.signature, old.docstring, old.summary);
    INSERT INTO code_symbols_fts(rowid, name, qualified_name, signature, docstring, summary)
    VALUES (new.rowid, new.name, new.qualified_name, new.signature, new.docstring, new.summary);
END;

-- code_content_fts
CREATE TRIGGER IF NOT EXISTS code_content_ai AFTER INSERT ON code_content_chunks BEGIN
    INSERT INTO code_content_fts(rowid, content, file_path, language)
    VALUES (new.rowid, new.content, new.file_path, new.language);
END;
CREATE TRIGGER IF NOT EXISTS code_content_ad AFTER DELETE ON code_content_chunks BEGIN
    INSERT INTO code_content_fts(code_content_fts, rowid, content, file_path, language)
    VALUES ('delete', old.rowid, old.content, old.file_path, old.language);
END;
CREATE TRIGGER IF NOT EXISTS code_content_au AFTER UPDATE ON code_content_chunks BEGIN
    INSERT INTO code_content_fts(code_content_fts, rowid, content, file_path, language)
    VALUES ('delete', old.rowid, old.content, old.file_path, old.language);
    INSERT INTO code_content_fts(rowid, content, file_path, language)
    VALUES (new.rowid, new.content, new.file_path, new.language);
END;
```

**Indexes** (11 total):
```sql
CREATE INDEX IF NOT EXISTS idx_cs_project ON code_symbols(project_id);
CREATE INDEX IF NOT EXISTS idx_cs_file ON code_symbols(project_id, file_path);
CREATE INDEX IF NOT EXISTS idx_cs_name ON code_symbols(name);
CREATE INDEX IF NOT EXISTS idx_cs_qualified ON code_symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_cs_kind ON code_symbols(kind);
CREATE INDEX IF NOT EXISTS idx_cs_parent ON code_symbols(parent_symbol_id);
CREATE INDEX IF NOT EXISTS idx_cif_project ON code_indexed_files(project_id);
CREATE INDEX IF NOT EXISTS idx_ccc_project ON code_content_chunks(project_id);
CREATE INDEX IF NOT EXISTS idx_ccc_file ON code_content_chunks(project_id, file_path);
CREATE INDEX IF NOT EXISTS idx_savings_ledger_created ON savings_ledger(created_at);
CREATE INDEX IF NOT EXISTS idx_savings_ledger_project_cat ON savings_ledger(project_id, category);
```

**Schema version tracking:**
```sql
CREATE TABLE IF NOT EXISTS gcode_schema (version INTEGER NOT NULL);
```
Insert version 1 if table is empty. On startup, check version matches `SCHEMA_VERSION` const. If version < expected, run ALTER TABLE migrations. If version > expected, warn but continue.

**Implementation note:** For embedded-store prototypes, run schema setup in one transaction. Triggers whose bodies contain semicolons must be executed as complete statements, not split on `;`.

### 2. Update `src/db.rs`

In `open_readwrite()`, after setting WAL mode and busy_timeout:
```rust
// Create parent directories if needed (standalone mode)
if let Some(parent) = db_path.parent() {
    std::fs::create_dir_all(parent)?;
}

let conn = Connection::open(db_path)?;
// ... WAL mode, busy_timeout ...

// Ensure schema exists (idempotent)
schema::ensure_schema(&conn)?;
```

### 3. Update `src/main.rs`

Add `mod schema;` to the module declarations.

## Verification

1. Delete `~/.gobby/gobby-hub.db`, run `gcode index .` — should create DB, all tables, indexes, FTS. `gcode search "query"` works.
2. Run `gcode status` — shows schema version and project stats.
3. Start gobby daemon after gcode created DB — daemon finds existing tables, no errors.
4. `cargo test` — all 31 tests still pass.
5. `cargo test --no-default-features` — passes without embeddings.

## Schema Parity Source

The CREATE TABLE definitions MUST exactly match gobby's Python BASELINE_SCHEMA:
- **File:** `src/gobby/storage/baseline_schema.sql` (in gobby repo)
- **Migrations:** `src/gobby/storage/migrations.py` (trigger helpers at lines 64-116)
- **Baseline version:** 175

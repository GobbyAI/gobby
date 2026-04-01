use anyhow::Context as _;
use rusqlite::Connection;

/// Current schema version for gcode-created databases.
const SCHEMA_VERSION: i64 = 2;

/// Ensure all required tables exist for gcode to operate standalone.
///
/// Safety: if `code_symbols` already exists (gobby or a prior gcode run created
/// the schema), this returns immediately without modifying anything.
pub fn ensure_schema(conn: &Connection) -> anyhow::Result<()> {
    let table_exists: bool = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_symbols')",
            [],
            |row| row.get(0),
        )
        .unwrap_or(false);

    if table_exists {
        // Run migrations on gcode-owned DBs (has gcode_schema table)
        migrate(conn)?;
        return Ok(());
    }

    // Fresh database — create everything in a single transaction.
    let tx = conn.unchecked_transaction()?;

    // ── Base tables ─────────────────────────────────────────────────
    tx.execute_batch(
        "CREATE TABLE code_symbols (
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

        CREATE TABLE code_indexed_files (
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

        CREATE TABLE code_content_chunks (
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

        CREATE TABLE code_indexed_projects (
            id TEXT PRIMARY KEY,
            root_path TEXT NOT NULL,
            total_files INTEGER NOT NULL DEFAULT 0,
            total_symbols INTEGER NOT NULL DEFAULT 0,
            last_indexed_at TEXT,
            index_duration_ms INTEGER,
            total_eligible_files INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE savings_ledger (
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
        );",
    )
    .context("failed to create base tables")?;

    // ── FTS5 virtual tables ─────────────────────────────────────────
    tx.execute_batch(
        "CREATE VIRTUAL TABLE code_symbols_fts USING fts5(
            name, qualified_name, signature, docstring, summary,
            content='code_symbols', content_rowid='rowid'
        );

        CREATE VIRTUAL TABLE code_content_fts USING fts5(
            content, file_path, language,
            content='code_content_chunks', content_rowid='rowid'
        );",
    )
    .context("failed to create FTS5 virtual tables")?;

    // ── FTS triggers (individual statements — they contain semicolons) ──

    // code_symbols_fts triggers
    tx.execute(
        "CREATE TRIGGER code_symbols_ai AFTER INSERT ON code_symbols BEGIN
            INSERT INTO code_symbols_fts(rowid, name, qualified_name, signature, docstring, summary)
            VALUES (new.rowid, new.name, new.qualified_name, new.signature, new.docstring, new.summary);
        END;",
        [],
    )
    .context("failed to create code_symbols_ai trigger")?;

    tx.execute(
        "CREATE TRIGGER code_symbols_ad AFTER DELETE ON code_symbols BEGIN
            INSERT INTO code_symbols_fts(code_symbols_fts, rowid, name, qualified_name, signature, docstring, summary)
            VALUES ('delete', old.rowid, old.name, old.qualified_name, old.signature, old.docstring, old.summary);
        END;",
        [],
    )
    .context("failed to create code_symbols_ad trigger")?;

    tx.execute(
        "CREATE TRIGGER code_symbols_au AFTER UPDATE ON code_symbols BEGIN
            INSERT INTO code_symbols_fts(code_symbols_fts, rowid, name, qualified_name, signature, docstring, summary)
            VALUES ('delete', old.rowid, old.name, old.qualified_name, old.signature, old.docstring, old.summary);
            INSERT INTO code_symbols_fts(rowid, name, qualified_name, signature, docstring, summary)
            VALUES (new.rowid, new.name, new.qualified_name, new.signature, new.docstring, new.summary);
        END;",
        [],
    )
    .context("failed to create code_symbols_au trigger")?;

    // code_content_fts triggers
    tx.execute(
        "CREATE TRIGGER code_content_ai AFTER INSERT ON code_content_chunks BEGIN
            INSERT INTO code_content_fts(rowid, content, file_path, language)
            VALUES (new.rowid, new.content, new.file_path, new.language);
        END;",
        [],
    )
    .context("failed to create code_content_ai trigger")?;

    tx.execute(
        "CREATE TRIGGER code_content_ad AFTER DELETE ON code_content_chunks BEGIN
            INSERT INTO code_content_fts(code_content_fts, rowid, content, file_path, language)
            VALUES ('delete', old.rowid, old.content, old.file_path, old.language);
        END;",
        [],
    )
    .context("failed to create code_content_ad trigger")?;

    tx.execute(
        "CREATE TRIGGER code_content_au AFTER UPDATE ON code_content_chunks BEGIN
            INSERT INTO code_content_fts(code_content_fts, rowid, content, file_path, language)
            VALUES ('delete', old.rowid, old.content, old.file_path, old.language);
            INSERT INTO code_content_fts(rowid, content, file_path, language)
            VALUES (new.rowid, new.content, new.file_path, new.language);
        END;",
        [],
    )
    .context("failed to create code_content_au trigger")?;

    // ── Indexes ─────────────────────────────────────────────────────
    tx.execute_batch(
        "CREATE INDEX idx_cs_project ON code_symbols(project_id);
        CREATE INDEX idx_cs_file ON code_symbols(project_id, file_path);
        CREATE INDEX idx_cs_name ON code_symbols(name);
        CREATE INDEX idx_cs_qualified ON code_symbols(qualified_name);
        CREATE INDEX idx_cs_kind ON code_symbols(kind);
        CREATE INDEX idx_cs_parent ON code_symbols(parent_symbol_id);
        CREATE INDEX idx_cif_project ON code_indexed_files(project_id);
        CREATE INDEX idx_ccc_project ON code_content_chunks(project_id);
        CREATE INDEX idx_ccc_file ON code_content_chunks(project_id, file_path);
        CREATE INDEX idx_savings_ledger_created ON savings_ledger(created_at);
        CREATE INDEX idx_savings_ledger_project_cat ON savings_ledger(project_id, category);",
    )
    .context("failed to create indexes")?;

    // ── Schema version tracking ─────────────────────────────────────
    tx.execute_batch("CREATE TABLE gcode_schema (version INTEGER NOT NULL);")
        .context("failed to create gcode_schema table")?;
    tx.execute(
        "INSERT INTO gcode_schema (version) VALUES (?1)",
        [SCHEMA_VERSION],
    )
    .context("failed to insert schema version")?;

    tx.commit().context("failed to commit schema transaction")?;
    Ok(())
}

/// Run incremental migrations on gcode-owned standalone databases.
/// Skips silently for gobby-managed DBs (no `gcode_schema` table).
fn migrate(conn: &Connection) -> anyhow::Result<()> {
    let has_schema_table: bool = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='gcode_schema')",
            [],
            |row| row.get(0),
        )
        .unwrap_or(false);

    if !has_schema_table {
        return Ok(()); // gobby-managed DB — daemon handles migrations
    }

    let version: i64 = conn
        .query_row("SELECT version FROM gcode_schema", [], |row| row.get(0))
        .unwrap_or(0);

    if version < 2 {
        // v1 → v2: add total_eligible_files column
        let has_column: bool = conn
            .prepare("PRAGMA table_info(code_indexed_projects)")?
            .query_map([], |row| row.get::<_, String>(1))?
            .any(|name| name.as_deref() == Ok("total_eligible_files"));

        if !has_column {
            conn.execute_batch(
                "ALTER TABLE code_indexed_projects ADD COLUMN total_eligible_files INTEGER;",
            )?;
        }

        conn.execute("UPDATE gcode_schema SET version = 2", [])?;
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn open_memory_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")
            .unwrap();
        conn
    }

    #[test]
    fn test_ensure_schema_fresh_db() {
        let conn = open_memory_db();
        ensure_schema(&conn).unwrap();

        // Verify base tables exist
        let tables: Vec<String> = conn
            .prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            .unwrap()
            .query_map([], |row| row.get(0))
            .unwrap()
            .filter_map(|r| r.ok())
            .collect();

        assert!(tables.contains(&"code_symbols".to_string()));
        assert!(tables.contains(&"code_indexed_files".to_string()));
        assert!(tables.contains(&"code_content_chunks".to_string()));
        assert!(tables.contains(&"code_indexed_projects".to_string()));
        assert!(tables.contains(&"savings_ledger".to_string()));
        assert!(tables.contains(&"gcode_schema".to_string()));

        // Verify FTS virtual tables
        let vtables: Vec<String> = conn
            .prepare(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_fts' ORDER BY name",
            )
            .unwrap()
            .query_map([], |row| row.get(0))
            .unwrap()
            .filter_map(|r| r.ok())
            .collect();

        assert!(vtables.contains(&"code_symbols_fts".to_string()));
        assert!(vtables.contains(&"code_content_fts".to_string()));

        // Verify schema version
        let version: i64 = conn
            .query_row("SELECT version FROM gcode_schema", [], |row| row.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);

        // Verify indexes exist
        let index_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(index_count, 11);

        // Verify triggers exist
        let trigger_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(trigger_count, 6);
    }

    #[test]
    fn test_ensure_schema_skips_existing() {
        let conn = open_memory_db();

        // Simulate a gobby-owned DB by creating just code_symbols
        conn.execute_batch(
            "CREATE TABLE code_symbols (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
             file_path TEXT, name TEXT, qualified_name TEXT, kind TEXT, language TEXT,
             byte_start INTEGER, byte_end INTEGER, line_start INTEGER, line_end INTEGER,
             content_hash TEXT NOT NULL, created_at TEXT, updated_at TEXT);",
        )
        .unwrap();

        ensure_schema(&conn).unwrap();

        // gcode_schema should NOT exist — we skipped schema creation
        let has_gcode_schema: bool = conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='gcode_schema')",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(!has_gcode_schema);
    }

    #[test]
    fn test_ensure_schema_idempotent() {
        let conn = open_memory_db();
        ensure_schema(&conn).unwrap();
        // Second call detects existing tables and returns Ok
        ensure_schema(&conn).unwrap();
    }
}

//! Full and incremental indexing orchestrator.
//!
//! Writes symbols, files, and content chunks to SQLite. External sync
//! (Qdrant vectors, Neo4j graph) is handled by the Gobby daemon's sync worker,
//! which polls for files with `vectors_synced=0` / `graph_synced=0`.

use std::collections::HashMap;
use std::path::Path;
use std::time::Instant;

use rusqlite::Connection;

use crate::index::chunker;
use crate::index::hasher;
use crate::index::languages;
use crate::index::parser;
use crate::index::walker;
use crate::models::{IndexResult, IndexedFile, IndexedProject};
use crate::progress::ProgressBar;

/// Default exclude patterns (matching Python CodeIndexConfig defaults).
const DEFAULT_EXCLUDES: &[&str] = &[
    "node_modules",
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "target",
    ".next",
    ".nuxt",
    "coverage",
    ".cache",
];

/// Index a directory (full or incremental).
pub fn index_directory(
    conn: &Connection,
    root_path: &Path,
    project_id: &str,
    incremental: bool,
    quiet: bool,
) -> anyhow::Result<IndexResult> {
    let start = Instant::now();
    let mut result = IndexResult {
        project_id: project_id.to_string(),
        files_indexed: 0,
        files_skipped: 0,
        symbols_found: 0,
        errors: Vec::new(),
        duration_ms: 0,
    };

    let excludes: Vec<String> = DEFAULT_EXCLUDES.iter().map(|s| s.to_string()).collect();
    let (candidates, content_only) = walker::discover_files(root_path, &excludes);

    // Build current hash map for incremental detection
    let mut current_hashes: HashMap<String, String> = HashMap::new();
    let stale: Option<HashMap<String, ()>> = if incremental {
        for path in &candidates {
            if let Ok(rel) = relative_path(path, root_path)
                && let Ok(h) = hasher::file_content_hash(path)
            {
                current_hashes.insert(rel, h);
            }
        }
        Some(get_stale_files(conn, project_id, &current_hashes))
    } else {
        None
    };

    // Clean orphans (SQLite only — daemon handles Neo4j/Qdrant cleanup)
    if incremental && !current_hashes.is_empty() {
        let orphans = get_orphan_files(conn, project_id, &current_hashes);
        for orphan in &orphans {
            delete_file_sqlite_data(conn, project_id, orphan);
        }
    }

    // Index each candidate file
    let eligible_files = candidates.len() + content_only.len();
    let mut progress = ProgressBar::new(eligible_files, quiet);

    for path in &candidates {
        let rel = match relative_path(path, root_path) {
            Ok(r) => r,
            Err(_) => continue,
        };

        progress.tick(&rel);

        if let Some(ref stale_map) = stale {
            if !stale_map.contains_key(&rel) {
                result.files_skipped += 1;
                continue;
            }
        }

        match index_file(conn, path, project_id, root_path, &excludes) {
            Some(count) => {
                result.files_indexed += 1;
                result.symbols_found += count;
            }
            None => {
                result.files_skipped += 1;
            }
        }
    }

    // Index content-only files
    for path in &content_only {
        let rel = relative_path(path, root_path).unwrap_or_default();
        progress.tick(&rel);
        index_content_only(conn, path, project_id, root_path);
    }

    progress.finish();

    let elapsed_ms = start.elapsed().as_millis() as u64;
    result.duration_ms = elapsed_ms;

    // Update project stats
    let total_files = count_rows(conn, "code_indexed_files", project_id);
    let total_symbols = count_rows(conn, "code_symbols", project_id);

    upsert_project_stats(
        conn,
        &IndexedProject {
            id: project_id.to_string(),
            root_path: root_path.to_string_lossy().to_string(),
            total_files,
            total_symbols,
            last_indexed_at: epoch_secs_str(),
            index_duration_ms: elapsed_ms,
            total_eligible_files: Some(eligible_files),
        },
    );

    Ok(result)
}

/// Index specific changed files.
pub fn index_files(
    conn: &Connection,
    root_path: &Path,
    project_id: &str,
    file_paths: &[String],
) -> anyhow::Result<IndexResult> {
    let start = Instant::now();
    let mut result = IndexResult {
        project_id: project_id.to_string(),
        files_indexed: 0,
        files_skipped: 0,
        symbols_found: 0,
        errors: Vec::new(),
        duration_ms: 0,
    };

    let excludes: Vec<String> = DEFAULT_EXCLUDES.iter().map(|s| s.to_string()).collect();

    for fp in file_paths {
        let abs = if Path::new(fp).is_absolute() {
            std::path::PathBuf::from(fp)
        } else {
            root_path.join(fp)
        };

        if !abs.exists() {
            // File deleted — clean up SQLite (daemon handles external cleanup)
            delete_file_sqlite_data(conn, project_id, fp);
            continue;
        }

        if let Some(count) = index_file(conn, &abs, project_id, root_path, &excludes) {
            result.files_indexed += 1;
            result.symbols_found += count;
        }
    }

    result.duration_ms = start.elapsed().as_millis() as u64;
    Ok(result)
}

/// Index a single file. Returns symbol count or None if skipped.
fn index_file(
    conn: &Connection,
    file_path: &Path,
    project_id: &str,
    root_path: &Path,
    exclude_patterns: &[String],
) -> Option<usize> {
    let rel = relative_path(file_path, root_path).ok()?;

    let parse_result = parser::parse_file(file_path, project_id, root_path, exclude_patterns)?;

    if parse_result.symbols.is_empty() {
        return Some(0);
    }

    let count = parse_result.symbols.len();

    // Detect optional columns/tables (daemon migration)
    let has_imports_table = has_table(conn, "code_imports");
    let has_calls_table = has_table(conn, "code_calls");
    let has_graph_synced = has_graph_synced_column(conn);
    let has_vectors_synced = has_vectors_synced_column(conn);

    // SQLite writes (transactional)
    let tx = conn.unchecked_transaction().ok()?;

    delete_file_sqlite_data(&tx, project_id, &rel);
    upsert_symbols(&tx, &parse_result.symbols);

    let language = languages::detect_language(&file_path.to_string_lossy()).unwrap_or("unknown");
    let h = hasher::file_content_hash(file_path).unwrap_or_default();
    let size = file_path.metadata().map(|m| m.len()).unwrap_or(0);

    upsert_file(
        &tx,
        &IndexedFile {
            id: IndexedFile::make_id(project_id, &rel),
            project_id: project_id.to_string(),
            file_path: rel.clone(),
            language: language.to_string(),
            content_hash: h,
            symbol_count: count,
            byte_size: size as usize,
            indexed_at: epoch_secs_str(),
        },
        has_graph_synced,
        has_vectors_synced,
    );

    // Write import/call relations to SQLite (if daemon migration v183 applied)
    if has_imports_table {
        upsert_imports(&tx, project_id, &rel, &parse_result.imports);
    }
    if has_calls_table {
        upsert_calls(&tx, project_id, &rel, &parse_result.calls);
    }

    let chunks =
        chunker::chunk_file_content(&parse_result.source, &rel, project_id, Some(language));
    if !chunks.is_empty() {
        upsert_content_chunks(&tx, &chunks);
    }

    tx.commit().ok()?;

    Some(count)
}

/// Index content-only file (no AST, just chunks).
fn index_content_only(conn: &Connection, path: &Path, project_id: &str, root_path: &Path) {
    let rel = match relative_path(path, root_path) {
        Ok(r) => r,
        Err(_) => return,
    };

    let meta = match path.metadata() {
        Ok(m) if m.len() > 0 && m.len() <= 10 * 1024 * 1024 => m,
        _ => return,
    };

    let source = match std::fs::read(path) {
        Ok(s) => s,
        Err(_) => return,
    };

    // Skip binary
    if source[..source.len().min(8192)].contains(&0) {
        return;
    }

    // Clear old chunks
    let _ = conn.execute(
        "DELETE FROM code_content_chunks WHERE project_id = ?1 AND file_path = ?2",
        rusqlite::params![project_id, &rel],
    );

    let lang = path.extension().map(|e| e.to_string_lossy().to_string());
    let chunks = chunker::chunk_file_content(&source, &rel, project_id, lang.as_deref());
    if !chunks.is_empty() {
        upsert_content_chunks(conn, &chunks);
    }

    let _ = meta; // used for size check above
}

/// Invalidate all index data for a project.
pub fn invalidate(
    conn: &Connection,
    project_id: &str,
    daemon_url: Option<&str>,
) -> anyhow::Result<()> {
    // Notify daemon FIRST — it reads project stats from the same SQLite
    // to know what to clean from Neo4j/Qdrant.
    if let Some(url) = daemon_url {
        notify_daemon_invalidate(url, project_id);
    }

    conn.execute(
        "DELETE FROM code_symbols WHERE project_id = ?1",
        rusqlite::params![project_id],
    )?;
    conn.execute(
        "DELETE FROM code_indexed_files WHERE project_id = ?1",
        rusqlite::params![project_id],
    )?;
    conn.execute(
        "DELETE FROM code_content_chunks WHERE project_id = ?1",
        rusqlite::params![project_id],
    )?;
    conn.execute(
        "DELETE FROM code_indexed_projects WHERE id = ?1",
        rusqlite::params![project_id],
    )?;
    eprintln!("Invalidated code index for project {project_id}");

    Ok(())
}

/// POST to the Gobby daemon requesting Neo4j/Qdrant cleanup for a project.
/// Fire-and-forget: warns on failure, never errors.
fn notify_daemon_invalidate(base_url: &str, project_id: &str) {
    let client = match reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .build()
    {
        Ok(c) => c,
        Err(_) => return,
    };

    let base = base_url.trim_end_matches('/');
    let url = format!("{base}/api/code-index/invalidate");
    match client
        .post(&url)
        .json(&serde_json::json!({"project_id": project_id}))
        .send()
    {
        Ok(resp) if !resp.status().is_success() => {
            eprintln!("Warning: daemon invalidate returned {}", resp.status());
        }
        Err(e) => {
            eprintln!("Warning: could not notify daemon: {e}");
        }
        _ => {}
    }
}

// ── SQLite helpers ─────────────────────────────────────────────────────

fn upsert_symbols(conn: &Connection, symbols: &[crate::models::Symbol]) {
    let now = epoch_secs_str();
    for sym in symbols {
        let _ = conn.execute(
            "INSERT INTO code_symbols (
                id, project_id, file_path, name, qualified_name,
                kind, language, byte_start, byte_end,
                line_start, line_end, signature, docstring,
                parent_symbol_id, content_hash,
                created_at, updated_at
            ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, qualified_name=excluded.qualified_name,
                kind=excluded.kind, byte_start=excluded.byte_start,
                byte_end=excluded.byte_end, line_start=excluded.line_start,
                line_end=excluded.line_end, signature=excluded.signature,
                docstring=excluded.docstring, parent_symbol_id=excluded.parent_symbol_id,
                language=excluded.language, content_hash=excluded.content_hash,
                updated_at=excluded.updated_at",
            rusqlite::params![
                sym.id,
                sym.project_id,
                sym.file_path,
                sym.name,
                sym.qualified_name,
                sym.kind,
                sym.language,
                sym.byte_start as i64,
                sym.byte_end as i64,
                sym.line_start as i64,
                sym.line_end as i64,
                sym.signature,
                sym.docstring,
                sym.parent_symbol_id,
                sym.content_hash,
                &now,
                &now,
            ],
        );
    }
}

fn upsert_file(
    conn: &Connection,
    file: &IndexedFile,
    has_graph_synced: bool,
    has_vectors_synced: bool,
) {
    if has_graph_synced && has_vectors_synced {
        let _ = conn.execute(
            "INSERT INTO code_indexed_files (
                id, project_id, file_path, language, content_hash,
                symbol_count, byte_size, indexed_at, graph_synced, vectors_synced
            ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8, 0, 0)
            ON CONFLICT(id) DO UPDATE SET
                content_hash=excluded.content_hash,
                symbol_count=excluded.symbol_count,
                byte_size=excluded.byte_size,
                indexed_at=excluded.indexed_at,
                graph_synced=0,
                vectors_synced=0",
            rusqlite::params![
                file.id,
                file.project_id,
                file.file_path,
                file.language,
                file.content_hash,
                file.symbol_count as i64,
                file.byte_size as i64,
                file.indexed_at,
            ],
        );
    } else if has_graph_synced {
        let _ = conn.execute(
            "INSERT INTO code_indexed_files (
                id, project_id, file_path, language, content_hash,
                symbol_count, byte_size, indexed_at, graph_synced
            ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8, 0)
            ON CONFLICT(id) DO UPDATE SET
                content_hash=excluded.content_hash,
                symbol_count=excluded.symbol_count,
                byte_size=excluded.byte_size,
                indexed_at=excluded.indexed_at,
                graph_synced=0",
            rusqlite::params![
                file.id,
                file.project_id,
                file.file_path,
                file.language,
                file.content_hash,
                file.symbol_count as i64,
                file.byte_size as i64,
                file.indexed_at,
            ],
        );
    } else {
        let _ = conn.execute(
            "INSERT INTO code_indexed_files (
                id, project_id, file_path, language, content_hash,
                symbol_count, byte_size, indexed_at
            ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)
            ON CONFLICT(id) DO UPDATE SET
                content_hash=excluded.content_hash,
                symbol_count=excluded.symbol_count,
                byte_size=excluded.byte_size,
                indexed_at=excluded.indexed_at",
            rusqlite::params![
                file.id,
                file.project_id,
                file.file_path,
                file.language,
                file.content_hash,
                file.symbol_count as i64,
                file.byte_size as i64,
                file.indexed_at,
            ],
        );
    }
}

fn upsert_content_chunks(conn: &Connection, chunks: &[crate::models::ContentChunk]) {
    for chunk in chunks {
        let _ = conn.execute(
            "INSERT INTO code_content_chunks (
                id, project_id, file_path, chunk_index,
                line_start, line_end, content, language, created_at
            ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,
                line_start=excluded.line_start,
                line_end=excluded.line_end",
            rusqlite::params![
                chunk.id,
                chunk.project_id,
                chunk.file_path,
                chunk.chunk_index as i64,
                chunk.line_start as i64,
                chunk.line_end as i64,
                chunk.content,
                chunk.language,
                chunk.created_at,
            ],
        );
    }
}

fn upsert_project_stats(conn: &Connection, project: &IndexedProject) {
    let _ = conn.execute(
        "INSERT INTO code_indexed_projects (
            id, root_path, total_files, total_symbols,
            last_indexed_at, index_duration_ms, total_eligible_files
        ) VALUES (?1,?2,?3,?4,?5,?6,?7)
        ON CONFLICT(id) DO UPDATE SET
            root_path=excluded.root_path,
            total_files=excluded.total_files,
            total_symbols=excluded.total_symbols,
            last_indexed_at=excluded.last_indexed_at,
            index_duration_ms=excluded.index_duration_ms,
            total_eligible_files=excluded.total_eligible_files",
        rusqlite::params![
            project.id,
            project.root_path,
            project.total_files as i64,
            project.total_symbols as i64,
            project.last_indexed_at,
            project.index_duration_ms as i64,
            project.total_eligible_files.map(|n| n as i64),
        ],
    );
}

/// Delete SQLite data for a file.
fn delete_file_sqlite_data(conn: &Connection, project_id: &str, file_path: &str) {
    let _ = conn.execute(
        "DELETE FROM code_symbols WHERE project_id = ?1 AND file_path = ?2",
        rusqlite::params![project_id, file_path],
    );
    let _ = conn.execute(
        "DELETE FROM code_indexed_files WHERE project_id = ?1 AND file_path = ?2",
        rusqlite::params![project_id, file_path],
    );
    let _ = conn.execute(
        "DELETE FROM code_content_chunks WHERE project_id = ?1 AND file_path = ?2",
        rusqlite::params![project_id, file_path],
    );
    // Clean import/call tables if they exist (daemon migration v183)
    if has_table(conn, "code_imports") {
        let _ = conn.execute(
            "DELETE FROM code_imports WHERE project_id = ?1 AND source_file = ?2",
            rusqlite::params![project_id, file_path],
        );
    }
    if has_table(conn, "code_calls") {
        let _ = conn.execute(
            "DELETE FROM code_calls WHERE project_id = ?1 AND file_path = ?2",
            rusqlite::params![project_id, file_path],
        );
    }
}

/// Write import relations to SQLite (delete-then-insert per file).
fn upsert_imports(
    conn: &Connection,
    project_id: &str,
    file_path: &str,
    imports: &[crate::models::ImportRelation],
) {
    let _ = conn.execute(
        "DELETE FROM code_imports WHERE project_id = ?1 AND source_file = ?2",
        rusqlite::params![project_id, file_path],
    );
    for imp in imports {
        let _ = conn.execute(
            "INSERT OR IGNORE INTO code_imports (project_id, source_file, target_module) \
             VALUES (?1, ?2, ?3)",
            rusqlite::params![project_id, &imp.file_path, &imp.module_name],
        );
    }
}

/// Write call relations to SQLite (delete-then-insert per file).
fn upsert_calls(
    conn: &Connection,
    project_id: &str,
    file_path: &str,
    calls: &[crate::models::CallRelation],
) {
    let _ = conn.execute(
        "DELETE FROM code_calls WHERE project_id = ?1 AND file_path = ?2",
        rusqlite::params![project_id, file_path],
    );
    for call in calls {
        let _ = conn.execute(
            "INSERT OR IGNORE INTO code_calls \
             (project_id, caller_symbol_id, callee_name, file_path, line) \
             VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params![
                project_id,
                &call.caller_id,
                &call.callee_name,
                &call.file_path,
                call.line as i64,
            ],
        );
    }
}

/// Check if the code_indexed_files table has a graph_synced column (gobby-hub.db only).
fn has_graph_synced_column(conn: &Connection) -> bool {
    has_column(conn, "code_indexed_files", "graph_synced")
}

/// Check if the code_indexed_files table has a vectors_synced column (daemon migration v183).
fn has_vectors_synced_column(conn: &Connection) -> bool {
    has_column(conn, "code_indexed_files", "vectors_synced")
}

/// Check if a table has a specific column.
fn has_column(conn: &Connection, table: &str, column: &str) -> bool {
    let sql = format!("PRAGMA table_info({table})");
    conn.prepare(&sql)
        .ok()
        .and_then(|mut stmt| {
            stmt.query_map([], |row| row.get::<_, String>(1))
                .ok()
                .map(|names| names.flatten().any(|n| n == column))
        })
        .unwrap_or(false)
}

/// Check if a table exists in the database.
fn has_table(conn: &Connection, table: &str) -> bool {
    conn.query_row(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?1",
        rusqlite::params![table],
        |row| row.get::<_, i64>(0),
    )
    .unwrap_or(0)
        > 0
}

fn get_stale_files(
    conn: &Connection,
    project_id: &str,
    current_hashes: &HashMap<String, String>,
) -> HashMap<String, ()> {
    let mut stale = HashMap::new();

    // Create temp table for comparison
    let _ = conn.execute_batch(
        "CREATE TEMP TABLE IF NOT EXISTS _current_hashes \
         (file_path TEXT PRIMARY KEY, content_hash TEXT); \
         DELETE FROM _current_hashes;",
    );

    for (path, hash) in current_hashes {
        let _ = conn.execute(
            "INSERT INTO _current_hashes (file_path, content_hash) VALUES (?1, ?2)",
            rusqlite::params![path, hash],
        );
    }

    // Files with changed content or not yet indexed
    if let Ok(mut stmt) = conn.prepare(
        "SELECT ch.file_path FROM _current_hashes ch \
         LEFT JOIN code_indexed_files cf \
             ON cf.project_id = ?1 AND cf.file_path = ch.file_path \
         WHERE cf.file_path IS NULL OR cf.content_hash != ch.content_hash",
    ) && let Ok(rows) =
        stmt.query_map(rusqlite::params![project_id], |row| row.get::<_, String>(0))
    {
        for row in rows.flatten() {
            stale.insert(row, ());
        }
    }

    let _ = conn.execute_batch("DROP TABLE IF EXISTS _current_hashes;");
    stale
}

fn get_orphan_files(
    conn: &Connection,
    project_id: &str,
    current_hashes: &HashMap<String, String>,
) -> Vec<String> {
    let mut orphans = Vec::new();
    if let Ok(mut stmt) =
        conn.prepare("SELECT file_path FROM code_indexed_files WHERE project_id = ?1")
        && let Ok(rows) =
            stmt.query_map(rusqlite::params![project_id], |row| row.get::<_, String>(0))
    {
        for row in rows.flatten() {
            if !current_hashes.contains_key(&row) {
                orphans.push(row);
            }
        }
    }
    orphans
}

fn count_rows(conn: &Connection, table: &str, project_id: &str) -> usize {
    let sql = format!("SELECT COUNT(*) FROM {table} WHERE project_id = ?1");
    conn.query_row(&sql, rusqlite::params![project_id], |row| {
        row.get::<_, i64>(0)
    })
    .unwrap_or(0) as usize
}

fn relative_path(path: &Path, root: &Path) -> anyhow::Result<String> {
    let abs = path.canonicalize()?;
    let root_abs = root.canonicalize()?;
    Ok(abs.strip_prefix(&root_abs)?.to_string_lossy().to_string())
}

fn epoch_secs_str() -> String {
    use std::time::SystemTime;
    let secs = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    format!("{secs}")
}

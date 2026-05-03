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
    let import_context = parser::build_import_resolution_context(root_path, &candidates);

    // Build current hash map for incremental detection and orphan cleanup.
    let current_hashes = current_file_hashes(root_path, &candidates, &content_only);
    let stale: Option<HashMap<String, ()>> = if incremental {
        Some(get_stale_files(conn, project_id, &current_hashes))
    } else {
        None
    };

    // Clean orphans (SQLite only — daemon handles Neo4j/Qdrant cleanup)
    let orphans = get_orphan_files(conn, project_id, &current_hashes);
    for orphan in &orphans {
        delete_file_sqlite_data(conn, project_id, orphan);
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

        match index_file(
            conn,
            path,
            project_id,
            root_path,
            &excludes,
            &import_context,
        ) {
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
        if let Some(ref stale_map) = stale
            && !stale_map.contains_key(&rel)
        {
            result.files_skipped += 1;
            continue;
        }
        if index_content_only(conn, path, project_id, root_path) {
            result.files_indexed += 1;
        } else {
            result.files_skipped += 1;
        }
    }

    progress.finish();

    let elapsed_ms = start.elapsed().as_millis() as u64;
    result.duration_ms = elapsed_ms;

    refresh_project_stats(
        conn,
        root_path,
        project_id,
        elapsed_ms,
        Some(eligible_files),
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
    let (candidates, content_only) = walker::discover_files(root_path, &excludes);
    let import_context = parser::build_import_resolution_context(root_path, &candidates);

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

        if let Some(count) = index_file(
            conn,
            &abs,
            project_id,
            root_path,
            &excludes,
            &import_context,
        ) {
            result.files_indexed += 1;
            result.symbols_found += count;
        }
    }

    result.duration_ms = start.elapsed().as_millis() as u64;
    refresh_project_stats(
        conn,
        root_path,
        project_id,
        result.duration_ms,
        Some(candidates.len() + content_only.len()),
    );
    Ok(result)
}

/// Index a single file. Returns symbol count or None if skipped.
fn index_file(
    conn: &Connection,
    file_path: &Path,
    project_id: &str,
    root_path: &Path,
    exclude_patterns: &[String],
    import_context: &parser::ImportResolutionContext,
) -> Option<usize> {
    let rel = relative_path(file_path, root_path).ok()?;

    let parse_result = parser::parse_file(
        file_path,
        project_id,
        root_path,
        exclude_patterns,
        import_context,
    )?;

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
fn index_content_only(conn: &Connection, path: &Path, project_id: &str, root_path: &Path) -> bool {
    let rel = match relative_path(path, root_path) {
        Ok(r) => r,
        Err(_) => return false,
    };

    let meta = match path.metadata() {
        Ok(m) if m.len() > 0 && m.len() <= 10 * 1024 * 1024 => m,
        _ => return false,
    };

    let source = match std::fs::read(path) {
        Ok(s) => s,
        Err(_) => return false,
    };

    // Skip binary
    if source[..source.len().min(8192)].contains(&0) {
        return false;
    }

    // Clear old chunks
    let _ = conn.execute(
        "DELETE FROM code_content_chunks WHERE project_id = ?1 AND file_path = ?2",
        rusqlite::params![project_id, &rel],
    );

    let lang = path.extension().map(|e| e.to_string_lossy().to_string());
    let content_hash = hasher::file_content_hash(path).unwrap_or_default();
    upsert_file(
        conn,
        &IndexedFile {
            id: IndexedFile::make_id(project_id, &rel),
            project_id: project_id.to_string(),
            file_path: rel.clone(),
            language: lang.clone().unwrap_or_else(|| "unknown".to_string()),
            content_hash,
            symbol_count: 0,
            byte_size: meta.len() as usize,
            indexed_at: epoch_secs_str(),
        },
        has_graph_synced_column(conn),
        has_vectors_synced_column(conn),
    );

    let chunks = chunker::chunk_file_content(&source, &rel, project_id, lang.as_deref());
    if !chunks.is_empty() {
        upsert_content_chunks(conn, &chunks);
    }

    true
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

fn refresh_project_stats(
    conn: &Connection,
    root_path: &Path,
    project_id: &str,
    elapsed_ms: u64,
    total_eligible_files: Option<usize>,
) {
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
            total_eligible_files,
        },
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
             (project_id, caller_symbol_id, callee_symbol_id, callee_name, \
              callee_target_kind, callee_external_module, file_path, line) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            rusqlite::params![
                project_id,
                &call.caller_symbol_id,
                call.callee_symbol_id.as_deref().unwrap_or(""),
                &call.callee_name,
                call.callee_target_kind.as_str(),
                call.callee_external_module.as_deref().unwrap_or(""),
                &call.file_path,
                call.line as i64,
            ],
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::{CallRelation, CallTargetKind};

    #[test]
    fn upsert_calls_writes_new_contract_columns() {
        let conn = Connection::open_in_memory().expect("open sqlite");
        conn.execute_batch(
            "CREATE TABLE code_calls (
                project_id TEXT NOT NULL,
                caller_symbol_id TEXT NOT NULL,
                callee_symbol_id TEXT NOT NULL DEFAULT '',
                callee_name TEXT NOT NULL,
                callee_target_kind TEXT NOT NULL,
                callee_external_module TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL,
                line INTEGER NOT NULL
            );",
        )
        .expect("create table");

        let resolved = CallRelation::new(
            "caller-1".to_string(),
            "foo".to_string(),
            "src/main.py".to_string(),
            12,
        )
        .with_symbol_target("callee-1".to_string());
        let unresolved = CallRelation::new(
            "caller-2".to_string(),
            "bar".to_string(),
            "src/main.py".to_string(),
            18,
        );

        upsert_calls(
            &conn,
            "proj",
            "src/main.py",
            &[resolved.clone(), unresolved.clone()],
        );

        let mut stmt = conn
            .prepare(
                "SELECT caller_symbol_id, callee_symbol_id, callee_name, \
                        callee_target_kind, callee_external_module, file_path, line \
                 FROM code_calls ORDER BY line",
            )
            .expect("prepare select");
        let rows: Vec<(String, String, String, String, String, String, i64)> = stmt
            .query_map([], |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                    row.get(6)?,
                ))
            })
            .expect("query rows")
            .map(|row| row.expect("read row"))
            .collect();

        assert_eq!(
            rows[0],
            (
                "caller-1".to_string(),
                "callee-1".to_string(),
                "foo".to_string(),
                CallTargetKind::Symbol.as_str().to_string(),
                "".to_string(),
                "src/main.py".to_string(),
                12,
            )
        );
        assert_eq!(
            rows[1],
            (
                "caller-2".to_string(),
                "".to_string(),
                "bar".to_string(),
                CallTargetKind::Unresolved.as_str().to_string(),
                "".to_string(),
                "src/main.py".to_string(),
                18,
            )
        );
    }

    #[test]
    fn content_only_indexing_writes_indexed_file_row() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let file_path = tmp.path().join("README.md");
        std::fs::write(&file_path, "# Outline\n\ncontent").expect("write file");
        let conn = Connection::open_in_memory().expect("open sqlite");
        conn.execute_batch(
            "CREATE TABLE code_indexed_files (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                language TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                symbol_count INTEGER NOT NULL DEFAULT 0,
                byte_size INTEGER NOT NULL DEFAULT 0,
                indexed_at TEXT NOT NULL
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
                created_at TEXT
            );",
        )
        .expect("create schema");

        assert!(index_content_only(&conn, &file_path, "proj", tmp.path()));

        let row: (String, i64) = conn
            .query_row(
                "SELECT language, symbol_count FROM code_indexed_files WHERE project_id = 'proj' AND file_path = 'README.md'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .expect("indexed file row");
        assert_eq!(row, ("md".to_string(), 0));
    }

    #[test]
    fn index_files_refreshes_project_stats() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let file_path = tmp.path().join("lib.rs");
        std::fs::write(&file_path, "pub fn target() {}\n").expect("write file");
        let conn = Connection::open_in_memory().expect("open sqlite");
        crate::schema::ensure_schema(&conn).expect("schema");

        let result =
            index_files(&conn, tmp.path(), "proj", &["lib.rs".to_string()]).expect("index files");

        assert_eq!(result.files_indexed, 1);
        let stats: (i64, i64) = conn
            .query_row(
                "SELECT total_files, total_symbols FROM code_indexed_projects WHERE id = 'proj'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .expect("project stats");
        assert_eq!(stats, (1, 1));
    }

    #[test]
    fn index_directory_removes_orphans_on_incremental_refresh() {
        let tmp = tempfile::tempdir().expect("tempdir");
        std::fs::write(tmp.path().join("keep.rs"), "pub fn keep() {}\n").expect("write keep");
        std::fs::write(tmp.path().join("gone.rs"), "pub fn gone() {}\n").expect("write gone");
        let conn = Connection::open_in_memory().expect("open sqlite");
        crate::schema::ensure_schema(&conn).expect("schema");

        index_directory(&conn, tmp.path(), "proj", true, true).expect("initial index");
        std::fs::remove_file(tmp.path().join("gone.rs")).expect("remove gone");
        index_directory(&conn, tmp.path(), "proj", true, true).expect("refresh index");

        let gone_rows: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM code_indexed_files WHERE project_id = 'proj' AND file_path = 'gone.rs'",
                [],
                |row| row.get(0),
            )
            .expect("count gone");
        let keep_rows: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM code_indexed_files WHERE project_id = 'proj' AND file_path = 'keep.rs'",
                [],
                |row| row.get(0),
            )
            .expect("count keep");

        assert_eq!(gone_rows, 0);
        assert_eq!(keep_rows, 1);
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

fn current_file_hashes(
    root_path: &Path,
    candidates: &[std::path::PathBuf],
    content_only: &[std::path::PathBuf],
) -> HashMap<String, String> {
    let mut current_hashes = HashMap::new();
    for path in candidates.iter().chain(content_only.iter()) {
        if let Ok(rel) = relative_path(path, root_path) {
            let hash = hasher::file_content_hash(path).unwrap_or_default();
            current_hashes.insert(rel, hash);
        }
    }
    current_hashes
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

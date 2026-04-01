//! Full and incremental indexing orchestrator.
//! Ports logic from src/gobby/code_index/indexer.py.

use std::collections::HashMap;
use std::path::Path;
use std::time::Instant;

use rusqlite::Connection;

use crate::config::QdrantConfig;
use crate::index::chunker;
use crate::index::hasher;
use crate::index::languages;
use crate::index::parser;
use crate::index::walker;
use crate::models::{IndexResult, IndexedFile, IndexedProject};
use crate::neo4j::Neo4jClient;
use crate::progress::ProgressBar;
use crate::search::semantic;

/// Why a file needs re-indexing.
#[derive(Debug)]
enum StaleReason {
    /// Content hash changed — full reindex needed.
    ContentChanged,
    /// Hash matches but graph_synced=0 — retry external writes only.
    GraphSyncPending,
}

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
    neo4j: Option<&Neo4jClient>,
    qdrant: Option<&QdrantConfig>,
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

    // Detect whether gobby-hub.db has the graph_synced column
    let has_graph_synced = has_graph_synced_column(conn);

    // Build current hash map for incremental detection
    let mut current_hashes: HashMap<String, String> = HashMap::new();
    let stale: Option<HashMap<String, StaleReason>> = if incremental {
        for path in &candidates {
            if let Ok(rel) = relative_path(path, root_path)
                && let Ok(h) = hasher::file_content_hash(path)
            {
                current_hashes.insert(rel, h);
            }
        }
        Some(get_stale_files(
            conn,
            project_id,
            &current_hashes,
            has_graph_synced,
        ))
    } else {
        None
    };

    // Clean orphans
    if incremental && !current_hashes.is_empty() {
        let orphans = get_orphan_files(conn, project_id, &current_hashes);
        for orphan in &orphans {
            delete_file_data(conn, project_id, orphan, neo4j, qdrant);
        }
    }

    // Ensure Qdrant collection exists (only when Gobby is installed and Qdrant configured)
    if let Some(config) = qdrant {
        let collection = format!("{}{}", config.collection_prefix, project_id);
        if let Err(e) = crate::search::semantic::ensure_collection(config, &collection) {
            if !quiet {
                eprintln!("Warning: failed to ensure Qdrant collection: {e}");
            }
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

        let graph_sync_only = match &stale {
            Some(stale_map) => match stale_map.get(&rel) {
                None => {
                    result.files_skipped += 1;
                    continue;
                }
                Some(StaleReason::GraphSyncPending) => true,
                Some(StaleReason::ContentChanged) => false,
            },
            None => false, // full reindex
        };

        match index_file(
            conn,
            path,
            project_id,
            root_path,
            &excludes,
            neo4j,
            qdrant,
            graph_sync_only,
            has_graph_synced,
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
    neo4j: Option<&Neo4jClient>,
    qdrant: Option<&QdrantConfig>,
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
    let has_graph_synced = has_graph_synced_column(conn);

    for fp in file_paths {
        let abs = if Path::new(fp).is_absolute() {
            std::path::PathBuf::from(fp)
        } else {
            root_path.join(fp)
        };

        if !abs.exists() {
            // File deleted — clean up
            delete_file_data(conn, project_id, fp, neo4j, qdrant);
            continue;
        }

        if let Some(count) = index_file(
            conn,
            &abs,
            project_id,
            root_path,
            &excludes,
            neo4j,
            qdrant,
            false, // always full reindex for targeted files
            has_graph_synced,
        ) {
            result.files_indexed += 1;
            result.symbols_found += count;
        }
    }

    result.duration_ms = start.elapsed().as_millis() as u64;
    Ok(result)
}

/// Index a single file. Returns symbol count or None if skipped.
///
/// When `graph_sync_only` is true, SQLite data is assumed correct — only
/// external writes (Neo4j/Qdrant) are retried and `graph_synced` is flipped.
#[allow(clippy::too_many_arguments)]
fn index_file(
    conn: &Connection,
    file_path: &Path,
    project_id: &str,
    root_path: &Path,
    exclude_patterns: &[String],
    neo4j: Option<&Neo4jClient>,
    qdrant: Option<&QdrantConfig>,
    graph_sync_only: bool,
    has_graph_synced: bool,
) -> Option<usize> {
    let rel = relative_path(file_path, root_path).ok()?;

    let parse_result = parser::parse_file(file_path, project_id, root_path, exclude_patterns)?;

    if parse_result.symbols.is_empty() {
        return Some(0);
    }

    let count = parse_result.symbols.len();

    // Phase 1: SQLite writes (transactional) — skip if graph_sync_only
    if !graph_sync_only {
        let tx = conn.unchecked_transaction().ok()?;

        delete_file_sqlite_data(&tx, project_id, &rel);
        upsert_symbols(&tx, &parse_result.symbols);

        let language =
            languages::detect_language(&file_path.to_string_lossy()).unwrap_or("unknown");
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
        );

        if let Ok(source) = std::fs::read(file_path) {
            let chunks = chunker::chunk_file_content(&source, &rel, project_id, Some(language));
            if !chunks.is_empty() {
                upsert_content_chunks(&tx, &chunks);
            }
        }

        tx.commit().ok()?;
    }

    // Phase 2: External writes (Neo4j/Qdrant) — outside transaction
    let mut external_ok = true;

    // Delete old external data (only on full reindex)
    if !graph_sync_only {
        if let Some(client) = neo4j {
            if crate::neo4j::delete_file_graph(client, project_id, &rel).is_err() {
                external_ok = false;
            }
        }
        if let Some(config) = qdrant {
            if let Ok(mut stmt) =
                conn.prepare("SELECT id FROM code_symbols WHERE project_id = ?1 AND file_path = ?2")
            {
                let ids: Vec<String> = stmt
                    .query_map(rusqlite::params![project_id, &rel], |row| row.get(0))
                    .ok()
                    .map(|rows| rows.filter_map(|r| r.ok()).collect())
                    .unwrap_or_default();
                if !ids.is_empty() {
                    let collection = format!("{}{}", config.collection_prefix, project_id);
                    if semantic::delete_vectors(config, &collection, &ids).is_err() {
                        external_ok = false;
                    }
                }
            }
        }
    }

    // Write new Qdrant vectors
    if let Some(config) = qdrant {
        let collection = format!("{}{}", config.collection_prefix, project_id);
        let texts: Vec<String> = parse_result
            .symbols
            .iter()
            .map(semantic::symbol_embed_text)
            .collect();
        let embeddings = semantic::embed_texts(&texts, false);
        let points: Vec<(String, Vec<f32>)> = parse_result
            .symbols
            .iter()
            .zip(embeddings)
            .filter_map(|(sym, emb)| Some((sym.id.clone(), emb?)))
            .collect();
        if !points.is_empty() && semantic::upsert_vectors(config, &collection, &points).is_err() {
            external_ok = false;
        }
    }

    // Write new Neo4j graph edges
    if let Some(client) = neo4j {
        if crate::neo4j::write_defines(client, project_id, &rel, &parse_result.symbols).is_err()
            || crate::neo4j::write_calls(client, project_id, &parse_result.calls).is_err()
            || crate::neo4j::write_imports(client, project_id, &parse_result.imports).is_err()
        {
            external_ok = false;
        }
    }

    // Flip graph_synced based on external write success
    if has_graph_synced {
        set_graph_synced(conn, project_id, &rel, external_ok);
    }

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
                parent_symbol_id, content_hash, summary,
                created_at, updated_at
            ) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18)
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
                sym.summary,
                &now,
                &now,
            ],
        );
    }
}

fn upsert_file(conn: &Connection, file: &IndexedFile, has_graph_synced: bool) {
    if has_graph_synced {
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

/// Delete all data for a file from all stores (SQLite, Neo4j, Qdrant).
/// Used for orphan cleanup where we want everything gone.
fn delete_file_data(
    conn: &Connection,
    project_id: &str,
    file_path: &str,
    neo4j: Option<&Neo4jClient>,
    qdrant: Option<&QdrantConfig>,
) {
    // Delete graph data first
    if let Some(client) = neo4j {
        let _ = crate::neo4j::delete_file_graph(client, project_id, file_path);
    }

    // Delete Qdrant vectors for this file's symbols (must query IDs before deleting from SQLite)
    if let Some(config) = qdrant {
        if let Ok(mut stmt) =
            conn.prepare("SELECT id FROM code_symbols WHERE project_id = ?1 AND file_path = ?2")
        {
            let ids: Vec<String> = stmt
                .query_map(rusqlite::params![project_id, file_path], |row| row.get(0))
                .ok()
                .map(|rows| rows.filter_map(|r| r.ok()).collect())
                .unwrap_or_default();
            if !ids.is_empty() {
                let collection = format!("{}{}", config.collection_prefix, project_id);
                let _ = semantic::delete_vectors(config, &collection, &ids);
            }
        }
    }

    delete_file_sqlite_data(conn, project_id, file_path);
}

/// Delete only SQLite data for a file. Safe to call inside a transaction.
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
}

/// Check if the code_indexed_files table has a graph_synced column (gobby-hub.db only).
fn has_graph_synced_column(conn: &Connection) -> bool {
    conn.prepare("PRAGMA table_info(code_indexed_files)")
        .ok()
        .and_then(|mut stmt| {
            stmt.query_map([], |row| row.get::<_, String>(1))
                .ok()
                .map(|names| names.flatten().any(|n| n == "graph_synced"))
        })
        .unwrap_or(false)
}

/// Set graph_synced flag for a file after external writes complete.
fn set_graph_synced(conn: &Connection, project_id: &str, file_path: &str, synced: bool) {
    let _ = conn.execute(
        "UPDATE code_indexed_files SET graph_synced = ?3 \
         WHERE project_id = ?1 AND file_path = ?2",
        rusqlite::params![project_id, file_path, synced as i32],
    );
}

fn get_stale_files(
    conn: &Connection,
    project_id: &str,
    current_hashes: &HashMap<String, String>,
    has_graph_synced: bool,
) -> HashMap<String, StaleReason> {
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
            stale.insert(row, StaleReason::ContentChanged);
        }
    }

    // Files where hash matches but graph_synced=0 (external writes pending)
    if has_graph_synced
        && let Ok(mut stmt) = conn.prepare(
            "SELECT cf.file_path FROM code_indexed_files cf \
             JOIN _current_hashes ch ON cf.file_path = ch.file_path \
             WHERE cf.project_id = ?1 \
               AND cf.content_hash = ch.content_hash \
               AND cf.graph_synced = 0",
        )
        && let Ok(rows) =
            stmt.query_map(rusqlite::params![project_id], |row| row.get::<_, String>(0))
    {
        for row in rows.flatten() {
            // ContentChanged takes priority if already present
            stale.entry(row).or_insert(StaleReason::GraphSyncPending);
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

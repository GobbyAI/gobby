//! Full and incremental indexing orchestrator.
//!
//! Writes symbols, files, and content chunks to the PostgreSQL hub. External sync
//! (Qdrant vectors, FalkorDB graph) is handled by the Gobby daemon's sync worker,
//! which polls for files with `vectors_synced=false` / `graph_synced=false`.

use std::collections::HashMap;
use std::path::Path;
use std::time::Instant;

use postgres::{Client, GenericClient};

use crate::index::chunker;
use crate::index::hasher;
use crate::index::languages;
use crate::index::parser;
use crate::index::semantic::{self, SemanticCallResolver};
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
    conn: &mut Client,
    root_path: &Path,
    project_id: &str,
    incremental: bool,
    quiet: bool,
    require_cpp_semantics: bool,
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
    let mut semantic_resolver =
        create_semantic_resolver_if_needed(root_path, &candidates, require_cpp_semantics)?;

    // Build current hash map for incremental detection and orphan cleanup.
    let current_hashes = current_file_hashes(root_path, &candidates, &content_only);
    let stale: Option<HashMap<String, ()>> = if incremental {
        Some(get_stale_files(conn, project_id, &current_hashes))
    } else {
        None
    };

    // Clean orphans from the hub; daemon handles FalkorDB/Qdrant cleanup.
    let orphans = get_orphan_files(conn, project_id, &current_hashes);
    for orphan in &orphans {
        delete_file_postgres_data(conn, project_id, orphan);
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

        if let Some(ref stale_map) = stale
            && !stale_map.contains_key(&rel)
        {
            result.files_skipped += 1;
            continue;
        }

        match index_file(
            conn,
            path,
            project_id,
            root_path,
            &excludes,
            &import_context,
            semantic_resolver.as_deref_mut(),
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
    conn: &mut Client,
    root_path: &Path,
    project_id: &str,
    file_paths: &[String],
    require_cpp_semantics: bool,
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
    let mut semantic_resolver =
        create_semantic_resolver_if_needed(root_path, &candidates, require_cpp_semantics)?;

    for fp in file_paths {
        let abs = if Path::new(fp).is_absolute() {
            std::path::PathBuf::from(fp)
        } else {
            root_path.join(fp)
        };

        if !abs.exists() {
            // File deleted — clean up hub rows (daemon handles external cleanup).
            delete_file_postgres_data(conn, project_id, fp);
            continue;
        }

        if let Some(count) = index_file(
            conn,
            &abs,
            project_id,
            root_path,
            &excludes,
            &import_context,
            semantic_resolver.as_deref_mut(),
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
    conn: &mut Client,
    file_path: &Path,
    project_id: &str,
    root_path: &Path,
    exclude_patterns: &[String],
    import_context: &parser::ImportResolutionContext,
    semantic_resolver: Option<&mut (dyn SemanticCallResolver + '_)>,
) -> Option<usize> {
    let rel = relative_path(file_path, root_path).ok()?;

    let parse_result = parser::parse_file_with_semantic(
        file_path,
        project_id,
        root_path,
        exclude_patterns,
        import_context,
        semantic_resolver,
    )?;

    let count = parse_result.symbols.len();

    // PostgreSQL hub writes (transactional).
    let mut tx = conn.transaction().ok()?;

    delete_file_postgres_data(&mut tx, project_id, &rel);
    upsert_symbols(&mut tx, &parse_result.symbols);

    let language = languages::detect_language(&file_path.to_string_lossy()).unwrap_or("unknown");
    let h = hasher::file_content_hash(file_path).unwrap_or_default();
    let size = file_path.metadata().map(|m| m.len()).unwrap_or(0);

    upsert_file(
        &mut tx,
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
    );

    upsert_imports(&mut tx, project_id, &rel, &parse_result.imports);
    upsert_calls(&mut tx, project_id, &rel, &parse_result.calls);

    let chunks =
        chunker::chunk_file_content(&parse_result.source, &rel, project_id, Some(language));
    if !chunks.is_empty() {
        upsert_content_chunks(&mut tx, &chunks);
    }

    tx.commit().ok()?;

    Some(count)
}

fn create_semantic_resolver_if_needed(
    root_path: &Path,
    candidates: &[std::path::PathBuf],
    require_cpp_semantics: bool,
) -> anyhow::Result<Option<Box<dyn SemanticCallResolver>>> {
    let has_cpp_candidate = candidates.iter().any(|path| {
        matches!(
            languages::detect_language(&path.to_string_lossy()),
            Some("c" | "cpp")
        )
    });
    if !has_cpp_candidate {
        return Ok(None);
    }
    semantic::create_cpp_semantic_resolver(root_path, require_cpp_semantics)
}

/// Index content-only file (no AST, just chunks).
fn index_content_only(conn: &mut Client, path: &Path, project_id: &str, root_path: &Path) -> bool {
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

    let lang = path.extension().map(|e| e.to_string_lossy().to_string());
    let content_hash = hasher::file_content_hash(path).unwrap_or_default();

    let mut tx = match conn.transaction() {
        Ok(tx) => tx,
        Err(_) => return false,
    };

    delete_file_postgres_data(&mut tx, project_id, &rel);
    upsert_file(
        &mut tx,
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
    );

    let chunks = chunker::chunk_file_content(&source, &rel, project_id, lang.as_deref());
    if !chunks.is_empty() {
        upsert_content_chunks(&mut tx, &chunks);
    }

    tx.commit().is_ok()
}

/// Invalidate all index data for a project.
pub fn invalidate(
    conn: &mut Client,
    project_id: &str,
    daemon_url: Option<&str>,
) -> anyhow::Result<()> {
    // Notify daemon FIRST — it reads project stats from the same hub
    // to know what to clean from FalkorDB/Qdrant.
    if let Some(url) = daemon_url {
        notify_daemon_invalidate(url, project_id);
    }

    conn.execute(
        "DELETE FROM code_symbols WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_indexed_files WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_content_chunks WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_imports WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_calls WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_indexed_projects WHERE id = $1",
        &[&project_id],
    )?;
    eprintln!("Invalidated code index for project {project_id}");

    Ok(())
}

/// POST to the Gobby daemon requesting FalkorDB/Qdrant cleanup for a project.
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

// ── PostgreSQL helpers ─────────────────────────────────────────────────

fn upsert_symbols(conn: &mut impl GenericClient, symbols: &[crate::models::Symbol]) {
    for sym in symbols {
        let _ = conn.execute(
            "INSERT INTO code_symbols (
                id, project_id, file_path, name, qualified_name,
                kind, language, byte_start, byte_end,
                line_start, line_end, signature, docstring,
                parent_symbol_id, content_hash, summary,
                created_at, updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,NOW(),NOW())
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, qualified_name=excluded.qualified_name,
                kind=excluded.kind, byte_start=excluded.byte_start,
                byte_end=excluded.byte_end, line_start=excluded.line_start,
                line_end=excluded.line_end, signature=excluded.signature,
                docstring=excluded.docstring, parent_symbol_id=excluded.parent_symbol_id,
                language=excluded.language, content_hash=excluded.content_hash,
                summary=CASE WHEN excluded.content_hash != code_symbols.content_hash
                             THEN NULL ELSE code_symbols.summary END,
                updated_at=NOW()",
            &[
                &sym.id,
                &sym.project_id,
                &sym.file_path,
                &sym.name,
                &sym.qualified_name,
                &sym.kind,
                &sym.language,
                &to_i32(sym.byte_start),
                &to_i32(sym.byte_end),
                &to_i32(sym.line_start),
                &to_i32(sym.line_end),
                &sym.signature,
                &sym.docstring,
                &sym.parent_symbol_id,
                &sym.content_hash,
                &sym.summary,
            ],
        );
    }
}

fn upsert_file(conn: &mut impl GenericClient, file: &IndexedFile) {
    let _ = conn.execute(
        "INSERT INTO code_indexed_files (
            id, project_id, file_path, language, content_hash,
            symbol_count, byte_size, graph_synced, vectors_synced,
            graph_sync_attempted_at, indexed_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,false,false,NULL,NOW())
        ON CONFLICT(id) DO UPDATE SET
            content_hash=excluded.content_hash,
            symbol_count=excluded.symbol_count,
            byte_size=excluded.byte_size,
            graph_synced=false,
            vectors_synced=false,
            graph_sync_attempted_at=NULL,
            indexed_at=NOW()",
        &[
            &file.id,
            &file.project_id,
            &file.file_path,
            &file.language,
            &file.content_hash,
            &to_i32(file.symbol_count),
            &to_i32(file.byte_size),
        ],
    );
}

fn upsert_content_chunks(conn: &mut impl GenericClient, chunks: &[crate::models::ContentChunk]) {
    for chunk in chunks {
        let _ = conn.execute(
            "INSERT INTO code_content_chunks (
                id, project_id, file_path, chunk_index,
                line_start, line_end, content, language, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW())
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,
                line_start=excluded.line_start,
                line_end=excluded.line_end",
            &[
                &chunk.id,
                &chunk.project_id,
                &chunk.file_path,
                &to_i32(chunk.chunk_index),
                &to_i32(chunk.line_start),
                &to_i32(chunk.line_end),
                &chunk.content,
                &chunk.language,
            ],
        );
    }
}

fn upsert_project_stats(conn: &mut impl GenericClient, project: &IndexedProject) {
    let _ = conn.execute(
        "INSERT INTO code_indexed_projects (
            id, root_path, total_files, total_symbols,
            last_indexed_at, index_duration_ms
        ) VALUES ($1,$2,$3,$4,NOW(),$5)
        ON CONFLICT(id) DO UPDATE SET
            root_path=excluded.root_path,
            total_files=excluded.total_files,
            total_symbols=excluded.total_symbols,
            last_indexed_at=excluded.last_indexed_at,
            index_duration_ms=excluded.index_duration_ms,
            updated_at=NOW()",
        &[
            &project.id,
            &project.root_path,
            &to_i32(project.total_files),
            &to_i32(project.total_symbols),
            &to_i32(project.index_duration_ms as usize),
        ],
    );
}

fn refresh_project_stats(
    conn: &mut Client,
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

/// Delete PostgreSQL hub data for a file.
fn delete_file_postgres_data(conn: &mut impl GenericClient, project_id: &str, file_path: &str) {
    let _ = conn.execute(
        "DELETE FROM code_symbols WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    );
    let _ = conn.execute(
        "DELETE FROM code_indexed_files WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    );
    let _ = conn.execute(
        "DELETE FROM code_content_chunks WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    );
    let _ = conn.execute(
        "DELETE FROM code_imports WHERE project_id = $1 AND source_file = $2",
        &[&project_id, &file_path],
    );
    let _ = conn.execute(
        "DELETE FROM code_calls WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    );
}

/// Write import relations to Postgres (delete-then-insert per file).
fn upsert_imports(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    imports: &[crate::models::ImportRelation],
) {
    let _ = conn.execute(
        "DELETE FROM code_imports WHERE project_id = $1 AND source_file = $2",
        &[&project_id, &file_path],
    );
    for imp in imports {
        let _ = conn.execute(
            "INSERT INTO code_imports (project_id, source_file, target_module)
             VALUES ($1, $2, $3)
             ON CONFLICT (project_id, source_file, target_module) DO NOTHING",
            &[&project_id, &imp.file_path, &imp.module_name],
        );
    }
}

/// Write call relations to Postgres (delete-then-insert per file).
fn upsert_calls(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    calls: &[crate::models::CallRelation],
) {
    let _ = conn.execute(
        "DELETE FROM code_calls WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    );
    for call in calls {
        let _ = conn.execute(
            "INSERT INTO code_calls
             (project_id, caller_symbol_id, callee_symbol_id, callee_name, \
              callee_target_kind, callee_external_module, file_path, line)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
             ON CONFLICT (
                project_id, caller_symbol_id, callee_symbol_id, callee_name,
                callee_target_kind, callee_external_module, file_path, line
             ) DO NOTHING",
            &[
                &project_id,
                &call.caller_symbol_id,
                &call.callee_symbol_id.as_deref().unwrap_or(""),
                &call.callee_name,
                &call.callee_target_kind.as_str(),
                &call.callee_external_module.as_deref().unwrap_or(""),
                &call.file_path,
                &to_i32(call.line),
            ],
        );
    }
}

fn get_stale_files(
    conn: &mut Client,
    project_id: &str,
    current_hashes: &HashMap<String, String>,
) -> HashMap<String, ()> {
    let mut stale = HashMap::new();
    let mut indexed = HashMap::new();
    if let Ok(rows) = conn.query(
        "SELECT file_path, content_hash FROM code_indexed_files WHERE project_id = $1",
        &[&project_id],
    ) {
        for row in rows {
            if let (Ok(file_path), Ok(content_hash)) = (
                row.try_get::<_, String>("file_path"),
                row.try_get::<_, String>("content_hash"),
            ) {
                indexed.insert(file_path, content_hash);
            }
        }
    }

    for (path, hash) in current_hashes {
        if indexed.get(path) != Some(hash) {
            stale.insert(path.clone(), ());
        }
    }
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
    conn: &mut Client,
    project_id: &str,
    current_hashes: &HashMap<String, String>,
) -> Vec<String> {
    let mut orphans = Vec::new();
    if let Ok(rows) = conn.query(
        "SELECT file_path FROM code_indexed_files WHERE project_id = $1",
        &[&project_id],
    ) {
        for row in rows {
            if let Ok(file_path) = row.try_get::<_, String>("file_path")
                && !current_hashes.contains_key(&file_path)
            {
                orphans.push(file_path);
            }
        }
    }
    orphans
}

fn count_rows(conn: &mut Client, table: &str, project_id: &str) -> usize {
    if !matches!(table, "code_indexed_files" | "code_symbols") {
        return 0;
    }
    let sql = format!("SELECT COUNT(*)::BIGINT AS count FROM {table} WHERE project_id = $1");
    conn.query_one(&sql, &[&project_id])
        .ok()
        .and_then(|row| row.try_get::<_, i64>("count").ok())
        .unwrap_or(0) as usize
}

fn to_i32(value: usize) -> i32 {
    value.try_into().unwrap_or(i32::MAX)
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

#[cfg(test)]
mod tests {
    use crate::models::{CallRelation, CallTargetKind};

    #[test]
    fn call_relation_contract_uses_empty_optional_storage_values() {
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

        assert_eq!(
            resolved.callee_symbol_id.as_deref().unwrap_or(""),
            "callee-1"
        );
        assert_eq!(unresolved.callee_symbol_id.as_deref().unwrap_or(""), "");
        assert_eq!(resolved.callee_target_kind, CallTargetKind::Symbol);
        assert_eq!(unresolved.callee_target_kind, CallTargetKind::Unresolved);
    }
}

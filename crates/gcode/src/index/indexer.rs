//! Full and incremental indexing orchestrator.
//!
//! Writes files, symbols, imports, calls, unresolved targets, and content chunks
//! to the PostgreSQL hub. External sync (Qdrant vectors, FalkorDB graph) is
//! delegated through projection sync status and handled outside this module.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::Context as _;
use postgres::{Client, GenericClient};
use serde::{Deserialize, Serialize};

use crate::config::Context;
use crate::db;
use crate::index::api;
use crate::index::chunker;
use crate::index::hasher;
use crate::index::languages;
use crate::index::parser;
use crate::index::semantic::{self, SemanticCallResolver};
use crate::index::walker;
use crate::models::{
    CallRelation, CallTargetKind, ContentChunk, ImportRelation, IndexedFile, IndexedProject,
    ParseResult, Symbol,
};
use crate::projection::sync::{
    self, ProjectionSyncRequest, ProjectionSyncStatus, ProjectionTarget,
};

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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexRequest {
    pub project_root: PathBuf,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub path_filter: Option<PathBuf>,
    #[serde(default)]
    pub explicit_files: Vec<PathBuf>,
    pub full: bool,
    pub require_cpp_semantics: bool,
    pub sync_projections: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexDurations {
    pub discovery_ms: u64,
    pub indexing_ms: u64,
    pub stats_ms: u64,
    pub total_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum IndexDegradation {
    FileIndexError { file_path: String, message: String },
    ProjectionSyncSkipped { reason: String },
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexOutcome {
    pub project_id: String,
    pub scanned_files: usize,
    pub indexed_files: usize,
    pub skipped_files: usize,
    pub symbols_indexed: usize,
    pub imports_indexed: usize,
    pub calls_indexed: usize,
    pub unresolved_targets_indexed: usize,
    pub chunks_indexed: usize,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub indexed_file_paths: Vec<String>,
    pub durations: IndexDurations,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub degraded: Vec<IndexDegradation>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub projection_sync: Option<ProjectionSyncStatus>,
}

impl IndexOutcome {
    fn new(project_id: &str) -> Self {
        Self {
            project_id: project_id.to_string(),
            ..Self::default()
        }
    }

    fn add_counts(&mut self, counts: FileIndexCounts) {
        self.indexed_files += counts.indexed_files;
        self.symbols_indexed += counts.symbols_indexed;
        self.imports_indexed += counts.imports_indexed;
        self.calls_indexed += counts.calls_indexed;
        self.unresolved_targets_indexed += counts.unresolved_targets_indexed;
        self.chunks_indexed += counts.chunks_indexed;
        if counts.indexed_files > 0 {
            self.indexed_file_paths.push(counts.file_path);
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct FileIndexCounts {
    file_path: String,
    indexed_files: usize,
    symbols_indexed: usize,
    imports_indexed: usize,
    calls_indexed: usize,
    unresolved_targets_indexed: usize,
    chunks_indexed: usize,
}

trait CodeFactSink {
    fn delete_file_facts(&mut self, project_id: &str, file_path: &str) -> anyhow::Result<()>;
    fn upsert_symbols(&mut self, symbols: &[Symbol]) -> anyhow::Result<usize>;
    fn upsert_file(&mut self, file: &IndexedFile) -> anyhow::Result<()>;
    fn upsert_imports(
        &mut self,
        project_id: &str,
        file_path: &str,
        imports: &[ImportRelation],
    ) -> anyhow::Result<usize>;
    fn upsert_calls(
        &mut self,
        project_id: &str,
        file_path: &str,
        calls: &[CallRelation],
    ) -> anyhow::Result<usize>;
    fn upsert_content_chunks(&mut self, chunks: &[ContentChunk]) -> anyhow::Result<usize>;
}

struct PostgresCodeFactSink<'a, C> {
    conn: &'a mut C,
}

impl<'a, C> PostgresCodeFactSink<'a, C> {
    fn new(conn: &'a mut C) -> Self {
        Self { conn }
    }
}

impl<C> CodeFactSink for PostgresCodeFactSink<'_, C>
where
    C: GenericClient,
{
    fn delete_file_facts(&mut self, project_id: &str, file_path: &str) -> anyhow::Result<()> {
        api::delete_file_facts(self.conn, project_id, file_path)
    }

    fn upsert_symbols(&mut self, symbols: &[Symbol]) -> anyhow::Result<usize> {
        api::upsert_symbols(self.conn, symbols)
    }

    fn upsert_file(&mut self, file: &IndexedFile) -> anyhow::Result<()> {
        api::upsert_file(self.conn, file)
    }

    fn upsert_imports(
        &mut self,
        project_id: &str,
        file_path: &str,
        imports: &[ImportRelation],
    ) -> anyhow::Result<usize> {
        api::upsert_imports(self.conn, project_id, file_path, imports)
    }

    fn upsert_calls(
        &mut self,
        project_id: &str,
        file_path: &str,
        calls: &[CallRelation],
    ) -> anyhow::Result<usize> {
        api::upsert_calls(self.conn, project_id, file_path, calls)
    }

    fn upsert_content_chunks(&mut self, chunks: &[ContentChunk]) -> anyhow::Result<usize> {
        api::upsert_content_chunks(self.conn, chunks)
    }
}

pub fn index_files(request: IndexRequest, ctx: &Context) -> anyhow::Result<IndexOutcome> {
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    index_files_with_connection(&mut conn, request, &ctx.project_id)
}

fn index_files_with_connection(
    conn: &mut Client,
    request: IndexRequest,
    project_id: &str,
) -> anyhow::Result<IndexOutcome> {
    if request.explicit_files.is_empty() {
        index_discovered_files(conn, &request, project_id)
    } else {
        index_explicit_files_with_connection(conn, &request, project_id)
    }
}

fn index_discovered_files(
    conn: &mut Client,
    request: &IndexRequest,
    project_id: &str,
) -> anyhow::Result<IndexOutcome> {
    let start = Instant::now();
    let discovery_start = Instant::now();
    let root_path = &request.project_root;
    let mut outcome = IndexOutcome::new(project_id);

    let excludes: Vec<String> = DEFAULT_EXCLUDES.iter().map(|s| s.to_string()).collect();
    let (mut candidates, mut content_only) = walker::discover_files(root_path, &excludes);
    if let Some(filter) = request.path_filter.as_deref() {
        candidates = filter_discovered_paths(root_path, filter, candidates);
        content_only = filter_discovered_paths(root_path, filter, content_only);
    }
    let import_context = parser::build_import_resolution_context(root_path, &candidates);
    let mut semantic_resolver =
        create_semantic_resolver_if_needed(root_path, &candidates, request.require_cpp_semantics)?;

    // Build current hash map for incremental detection and orphan cleanup.
    let current_hashes = current_file_hashes(root_path, &candidates, &content_only);
    let stale: Option<HashMap<String, ()>> = if !request.full {
        Some(get_stale_files(conn, project_id, &current_hashes))
    } else {
        None
    };

    // Clean orphans only during whole-project scans. Filtered scans do not know
    // about files outside the requested subtree.
    if request.path_filter.is_none() {
        let orphans = get_orphan_files(conn, project_id, &current_hashes);
        for orphan in &orphans {
            api::delete_file_facts(conn, project_id, orphan)?;
        }
    }

    let eligible_files = candidates.len() + content_only.len();
    outcome.scanned_files = eligible_files;
    outcome.durations.discovery_ms = discovery_start.elapsed().as_millis() as u64;

    let indexing_start = Instant::now();
    for path in &candidates {
        let rel = match relative_path(path, root_path) {
            Ok(r) => r,
            Err(_) => continue,
        };

        if let Some(ref stale_map) = stale
            && !stale_map.contains_key(&rel)
        {
            outcome.skipped_files += 1;
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
        )? {
            Some(counts) => outcome.add_counts(counts),
            None => {
                outcome.skipped_files += 1;
            }
        }
    }

    for path in &content_only {
        let rel = relative_path(path, root_path).unwrap_or_default();
        if let Some(ref stale_map) = stale
            && !stale_map.contains_key(&rel)
        {
            outcome.skipped_files += 1;
            continue;
        }
        match index_content_only(conn, path, project_id, root_path, &excludes)? {
            Some(counts) => outcome.add_counts(counts),
            None => outcome.skipped_files += 1,
        }
    }
    outcome.durations.indexing_ms = indexing_start.elapsed().as_millis() as u64;

    let stats_start = Instant::now();
    refresh_project_stats(
        conn,
        root_path,
        project_id,
        start.elapsed().as_millis() as u64,
        Some(eligible_files),
    );
    outcome.durations.stats_ms = stats_start.elapsed().as_millis() as u64;
    outcome.durations.total_ms = start.elapsed().as_millis() as u64;

    attach_projection_sync(&mut outcome, request);
    Ok(outcome)
}

fn index_explicit_files_with_connection(
    conn: &mut Client,
    request: &IndexRequest,
    project_id: &str,
) -> anyhow::Result<IndexOutcome> {
    let start = Instant::now();
    let discovery_start = Instant::now();
    let root_path = &request.project_root;
    let mut outcome = IndexOutcome::new(project_id);
    outcome.scanned_files = request.explicit_files.len();

    let excludes: Vec<String> = DEFAULT_EXCLUDES.iter().map(|s| s.to_string()).collect();
    let (candidates, content_only) = walker::discover_files(root_path, &excludes);
    let import_context = parser::build_import_resolution_context(root_path, &candidates);
    let mut routed_files = Vec::new();
    let mut ast_files = Vec::new();

    for fp in &request.explicit_files {
        let abs = if fp.is_absolute() {
            fp.clone()
        } else {
            root_path.join(fp)
        };

        if !abs.exists() {
            // File deleted — clean up hub rows (daemon handles external cleanup).
            let rel = requested_relative_path(root_path, fp);
            api::delete_file_facts(conn, project_id, &rel)?;
            continue;
        }

        match explicit_file_route(root_path, &abs, &excludes) {
            ExplicitFileRoute::Ast => {
                ast_files.push(abs.clone());
                routed_files.push((abs, ExplicitFileRoute::Ast));
            }
            ExplicitFileRoute::ContentOnly => {
                routed_files.push((abs, ExplicitFileRoute::ContentOnly));
            }
            ExplicitFileRoute::Skip => {
                outcome.skipped_files += 1;
            }
        }
    }

    let mut semantic_resolver =
        create_semantic_resolver_if_needed(root_path, &ast_files, request.require_cpp_semantics)?;
    outcome.durations.discovery_ms = discovery_start.elapsed().as_millis() as u64;

    let indexing_start = Instant::now();
    for (abs, route) in routed_files {
        match route {
            ExplicitFileRoute::Ast => {
                if let Some(count) = index_file(
                    conn,
                    &abs,
                    project_id,
                    root_path,
                    &excludes,
                    &import_context,
                    semantic_resolver.as_deref_mut(),
                )? {
                    outcome.add_counts(count);
                } else {
                    outcome.skipped_files += 1;
                }
            }
            ExplicitFileRoute::ContentOnly => {
                match index_content_only(conn, &abs, project_id, root_path, &excludes)? {
                    Some(counts) => outcome.add_counts(counts),
                    None => outcome.skipped_files += 1,
                }
            }
            _ => unreachable!("skip routes are filtered before indexing"),
        }
    }
    outcome.durations.indexing_ms = indexing_start.elapsed().as_millis() as u64;

    let stats_start = Instant::now();
    refresh_project_stats(
        conn,
        root_path,
        project_id,
        start.elapsed().as_millis() as u64,
        Some(candidates.len() + content_only.len()),
    );
    outcome.durations.stats_ms = stats_start.elapsed().as_millis() as u64;
    outcome.durations.total_ms = start.elapsed().as_millis() as u64;

    attach_projection_sync(&mut outcome, request);
    Ok(outcome)
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
) -> anyhow::Result<Option<FileIndexCounts>> {
    let rel = match relative_path(file_path, root_path) {
        Ok(rel) => rel,
        Err(_) => return Ok(None),
    };

    let Some(parse_result) = parser::parse_file_with_semantic(
        file_path,
        project_id,
        root_path,
        exclude_patterns,
        import_context,
        semantic_resolver,
    )?
    else {
        return Ok(None);
    };

    // PostgreSQL hub writes (transactional).
    let mut tx = conn
        .transaction()
        .context("start indexed file transaction")?;

    let language = languages::detect_language(&file_path.to_string_lossy()).unwrap_or("unknown");
    let h = hasher::file_content_hash(file_path).unwrap_or_default();
    let size = file_path.metadata().map(|m| m.len()).unwrap_or(0);
    let mut sink = PostgresCodeFactSink::new(&mut tx);
    let counts = write_parsed_file_facts(
        &mut sink,
        project_id,
        &rel,
        language,
        &h,
        size as usize,
        &parse_result,
    )?;

    tx.commit().context("commit indexed file transaction")?;

    Ok(Some(counts))
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ExplicitFileRoute {
    Ast,
    ContentOnly,
    Skip,
}

fn explicit_file_route(
    root_path: &Path,
    path: &Path,
    exclude_patterns: &[String],
) -> ExplicitFileRoute {
    match walker::classify_file(root_path, path, exclude_patterns) {
        Some(walker::FileClassification::Ast) => ExplicitFileRoute::Ast,
        Some(walker::FileClassification::ContentOnly) => ExplicitFileRoute::ContentOnly,
        None => ExplicitFileRoute::Skip,
    }
}

/// Index content-only file (no AST, just chunks).
fn index_content_only(
    conn: &mut Client,
    path: &Path,
    project_id: &str,
    root_path: &Path,
    exclude_patterns: &[String],
) -> anyhow::Result<Option<FileIndexCounts>> {
    if !walker::is_content_indexable(root_path, path, exclude_patterns) {
        return Ok(None);
    }

    let rel = match relative_path(path, root_path) {
        Ok(r) => r,
        Err(_) => return Ok(None),
    };

    let source = match std::fs::read(path) {
        Ok(s) => s,
        Err(_) => return Ok(None),
    };

    let lang = walker::content_language(path);
    let content_hash = hasher::file_content_hash(path).unwrap_or_default();

    let mut tx = conn
        .transaction()
        .context("start content-only file transaction")?;
    let mut sink = PostgresCodeFactSink::new(&mut tx);
    let counts = write_content_only_file_facts(
        &mut sink,
        project_id,
        &rel,
        &lang,
        &content_hash,
        source.len(),
        &source,
    )?;

    tx.commit()
        .context("commit content-only file transaction")?;
    Ok(Some(counts))
}

fn write_parsed_file_facts(
    sink: &mut impl CodeFactSink,
    project_id: &str,
    rel: &str,
    language: &str,
    content_hash: &str,
    byte_size: usize,
    parse_result: &ParseResult,
) -> anyhow::Result<FileIndexCounts> {
    sink.delete_file_facts(project_id, rel)?;
    let symbols_indexed = sink.upsert_symbols(&parse_result.symbols)?;
    sink.upsert_file(&IndexedFile {
        id: IndexedFile::make_id(project_id, rel),
        project_id: project_id.to_string(),
        file_path: rel.to_string(),
        language: language.to_string(),
        content_hash: content_hash.to_string(),
        symbol_count: parse_result.symbols.len(),
        byte_size,
        indexed_at: epoch_secs_str(),
    })?;
    let imports_indexed = sink.upsert_imports(project_id, rel, &parse_result.imports)?;
    let calls_indexed = sink.upsert_calls(project_id, rel, &parse_result.calls)?;
    let unresolved_targets_indexed = parse_result
        .calls
        .iter()
        .filter(|call| call.callee_target_kind == CallTargetKind::Unresolved)
        .count();
    let chunks = chunker::chunk_file_content(&parse_result.source, rel, project_id, Some(language));
    let chunks_indexed = if chunks.is_empty() {
        0
    } else {
        sink.upsert_content_chunks(&chunks)?
    };

    Ok(FileIndexCounts {
        file_path: rel.to_string(),
        indexed_files: 1,
        symbols_indexed,
        imports_indexed,
        calls_indexed,
        unresolved_targets_indexed,
        chunks_indexed,
    })
}

fn write_content_only_file_facts(
    sink: &mut impl CodeFactSink,
    project_id: &str,
    rel: &str,
    language: &str,
    content_hash: &str,
    byte_size: usize,
    source: &[u8],
) -> anyhow::Result<FileIndexCounts> {
    sink.delete_file_facts(project_id, rel)?;
    sink.upsert_file(&IndexedFile {
        id: IndexedFile::make_id(project_id, rel),
        project_id: project_id.to_string(),
        file_path: rel.to_string(),
        language: language.to_string(),
        content_hash: content_hash.to_string(),
        symbol_count: 0,
        byte_size,
        indexed_at: epoch_secs_str(),
    })?;
    let chunks = chunker::chunk_file_content(source, rel, project_id, Some(language));
    let chunks_indexed = if chunks.is_empty() {
        0
    } else {
        sink.upsert_content_chunks(&chunks)?
    };

    Ok(FileIndexCounts {
        file_path: rel.to_string(),
        indexed_files: 1,
        chunks_indexed,
        ..FileIndexCounts::default()
    })
}

fn filter_discovered_paths(
    root_path: &Path,
    path_filter: &Path,
    paths: Vec<PathBuf>,
) -> Vec<PathBuf> {
    let filter_abs = if path_filter.is_absolute() {
        path_filter.to_path_buf()
    } else {
        root_path.join(path_filter)
    };
    let filter_abs = filter_abs.canonicalize().unwrap_or(filter_abs);

    paths
        .into_iter()
        .filter(|path| {
            let path_abs = path.canonicalize().unwrap_or_else(|_| path.clone());
            path_abs == filter_abs || path_abs.starts_with(&filter_abs)
        })
        .collect()
}

fn requested_relative_path(root_path: &Path, requested_path: &Path) -> String {
    if requested_path.is_absolute() {
        return requested_path
            .strip_prefix(root_path)
            .unwrap_or(requested_path)
            .to_string_lossy()
            .to_string();
    }
    requested_path.to_string_lossy().to_string()
}

fn attach_projection_sync(outcome: &mut IndexOutcome, request: &IndexRequest) {
    if !request.sync_projections {
        return;
    }

    outcome.projection_sync = Some(sync::pending_after_code_fact_write(ProjectionSyncRequest {
        project_id: outcome.project_id.clone(),
        file_paths: outcome.indexed_file_paths.clone(),
        targets: vec![ProjectionTarget::Graph, ProjectionTarget::Vectors],
    }));
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

fn refresh_project_stats(
    conn: &mut Client,
    root_path: &Path,
    project_id: &str,
    elapsed_ms: u64,
    total_eligible_files: Option<usize>,
) {
    let total_files = count_rows(conn, "code_indexed_files", project_id);
    let total_symbols = count_rows(conn, "code_symbols", project_id);

    let _ = api::upsert_project_stats(
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
    use super::*;
    use crate::models::{CallRelation, CallTargetKind, ImportRelation, ParseResult, Symbol};
    use serde::Serialize;
    use serde::de::DeserializeOwned;
    use std::path::Path;
    use std::path::PathBuf;

    fn write_file(root: &Path, rel: &str, contents: &[u8]) {
        let path = root.join(rel);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("create parent");
        }
        std::fs::write(path, contents).expect("write file");
    }

    fn assert_cli_independent_contract<T>()
    where
        T: Serialize + DeserializeOwned,
    {
        let type_name = std::any::type_name::<T>();
        assert!(!type_name.contains("commands::"), "{type_name}");
        assert!(!type_name.contains("output::"), "{type_name}");
        assert!(!type_name.contains("clap"), "{type_name}");
    }

    #[test]
    fn library_api_is_cli_independent() {
        assert_cli_independent_contract::<IndexRequest>();
        assert_cli_independent_contract::<IndexOutcome>();
        assert_cli_independent_contract::<IndexDurations>();
        assert_cli_independent_contract::<IndexDegradation>();

        let request = IndexRequest {
            project_root: PathBuf::from("/tmp/project"),
            path_filter: Some(PathBuf::from("src")),
            explicit_files: vec![PathBuf::from("src/lib.rs")],
            full: true,
            require_cpp_semantics: false,
            sync_projections: true,
        };

        let json = serde_json::to_value(&request).expect("request serializes");
        assert_eq!(json["project_root"], "/tmp/project");
        assert_eq!(json["path_filter"], "src");
        assert_eq!(json["explicit_files"][0], "src/lib.rs");
    }

    #[test]
    fn invalidate_postgres_deletes_are_project_scoped() {
        let source = include_str!("indexer.rs");
        for expected in [
            "DELETE FROM code_symbols WHERE project_id = $1",
            "DELETE FROM code_indexed_files WHERE project_id = $1",
            "DELETE FROM code_content_chunks WHERE project_id = $1",
            "DELETE FROM code_imports WHERE project_id = $1",
            "DELETE FROM code_calls WHERE project_id = $1",
            "DELETE FROM code_indexed_projects WHERE id = $1",
        ] {
            assert!(
                source.contains(expected),
                "missing scoped delete: {expected}"
            );
        }
        let truncate_code = ["TRUNCATE", " code_"].concat();
        let drop_table = ["DROP", " TABLE"].concat();
        assert!(!source.contains(&truncate_code));
        assert!(!source.contains(&drop_table));
    }

    #[derive(Default)]
    struct RecordingCodeFactSink {
        writes: Vec<&'static str>,
        files: usize,
        symbols: usize,
        imports: usize,
        calls: usize,
        unresolved_targets: usize,
        chunks: usize,
    }

    impl CodeFactSink for RecordingCodeFactSink {
        fn delete_file_facts(&mut self, _project_id: &str, _file_path: &str) -> anyhow::Result<()> {
            self.writes.push("delete");
            Ok(())
        }

        fn upsert_symbols(&mut self, symbols: &[Symbol]) -> anyhow::Result<usize> {
            self.writes.push("symbols");
            self.symbols += symbols.len();
            Ok(symbols.len())
        }

        fn upsert_file(&mut self, _file: &IndexedFile) -> anyhow::Result<()> {
            self.writes.push("file");
            self.files += 1;
            Ok(())
        }

        fn upsert_imports(
            &mut self,
            _project_id: &str,
            _file_path: &str,
            imports: &[ImportRelation],
        ) -> anyhow::Result<usize> {
            self.writes.push("imports");
            self.imports += imports.len();
            Ok(imports.len())
        }

        fn upsert_calls(
            &mut self,
            _project_id: &str,
            _file_path: &str,
            calls: &[CallRelation],
        ) -> anyhow::Result<usize> {
            self.writes.push("calls");
            self.calls += calls.len();
            self.unresolved_targets += calls
                .iter()
                .filter(|call| call.callee_target_kind == CallTargetKind::Unresolved)
                .count();
            Ok(calls.len())
        }

        fn upsert_content_chunks(&mut self, chunks: &[ContentChunk]) -> anyhow::Result<usize> {
            self.writes.push("chunks");
            self.chunks += chunks.len();
            Ok(chunks.len())
        }
    }

    #[test]
    fn library_writes_all_code_facts() {
        let project_id = "project-1";
        let rel = "src/lib.rs";
        let source = b"use std::fmt;\nfn caller() {\n    missing();\n}\n";
        let caller_id = Symbol::make_id(project_id, rel, "caller", "function", 14);
        let parse_result = ParseResult {
            symbols: vec![Symbol {
                id: caller_id.clone(),
                project_id: project_id.to_string(),
                file_path: rel.to_string(),
                name: "caller".to_string(),
                qualified_name: "caller".to_string(),
                kind: "function".to_string(),
                language: "rust".to_string(),
                byte_start: 14,
                byte_end: 45,
                line_start: 2,
                line_end: 4,
                signature: Some("fn caller()".to_string()),
                docstring: None,
                parent_symbol_id: None,
                content_hash: "hash-1".to_string(),
                summary: None,
                created_at: String::new(),
                updated_at: String::new(),
            }],
            imports: vec![ImportRelation {
                file_path: rel.to_string(),
                module_name: "std::fmt".to_string(),
            }],
            calls: vec![CallRelation::new(
                caller_id,
                "missing".to_string(),
                rel.to_string(),
                3,
            )],
            source: source.to_vec(),
        };

        let mut sink = RecordingCodeFactSink::default();
        let counts = write_parsed_file_facts(
            &mut sink,
            project_id,
            rel,
            "rust",
            "hash-1",
            source.len(),
            &parse_result,
        )
        .expect("write parsed file facts");

        assert_eq!(
            sink.writes,
            vec!["delete", "symbols", "file", "imports", "calls", "chunks"]
        );
        assert_eq!(sink.files, 1);
        assert_eq!(sink.symbols, 1);
        assert_eq!(sink.imports, 1);
        assert_eq!(sink.calls, 1);
        assert_eq!(sink.unresolved_targets, 1);
        assert_eq!(sink.chunks, 1);
        assert_eq!(counts.indexed_files, 1);
        assert_eq!(counts.symbols_indexed, 1);
        assert_eq!(counts.imports_indexed, 1);
        assert_eq!(counts.calls_indexed, 1);
        assert_eq!(counts.unresolved_targets_indexed, 1);
        assert_eq!(counts.chunks_indexed, 1);
    }

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

    #[test]
    fn explicit_file_route_sends_unsupported_text_to_content_only() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        write_file(root, "src/lib.rs", b"fn main() {}\n");
        write_file(root, "notes.txt", b"plain notes\n");
        write_file(root, "Dockerfile", b"FROM rust:latest\n");
        write_file(root, "api_key.txt", b"secret-ish\n");
        write_file(root, "target/generated.txt", b"generated\n");
        write_file(root, "image.bin", b"PNG\0binary");

        let excludes: Vec<String> = DEFAULT_EXCLUDES.iter().map(|s| s.to_string()).collect();

        assert_eq!(
            explicit_file_route(root, &root.join("src/lib.rs"), &excludes),
            ExplicitFileRoute::Ast
        );
        assert_eq!(
            explicit_file_route(root, &root.join("notes.txt"), &excludes),
            ExplicitFileRoute::ContentOnly
        );
        assert_eq!(
            explicit_file_route(root, &root.join("Dockerfile"), &excludes),
            ExplicitFileRoute::ContentOnly
        );
        assert_eq!(
            explicit_file_route(root, &root.join("api_key.txt"), &excludes),
            ExplicitFileRoute::Skip
        );
        assert_eq!(
            explicit_file_route(root, &root.join("target/generated.txt"), &excludes),
            ExplicitFileRoute::Skip
        );
        assert_eq!(
            explicit_file_route(root, &root.join("image.bin"), &excludes),
            ExplicitFileRoute::Skip
        );
    }
}

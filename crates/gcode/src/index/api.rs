use postgres::GenericClient;
use serde::{Deserialize, Serialize};

pub use crate::index::indexer::{
    IndexDegradation, IndexDurations, IndexOutcome, IndexRequest, UnsupportedFileType, index_files,
    project_changed_since,
};

use crate::models::{
    CallRelation, ContentChunk, ImportRelation, IndexedFile, IndexedProject, Symbol,
};

const SYMBOL_UPSERT_BATCH_SIZE: usize = 500;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeFactWriteRequest {
    pub project_id: String,
    pub file_path: String,
    pub symbols: usize,
    pub imports: usize,
    pub calls: usize,
    pub chunks: usize,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeFactWriteSummary {
    pub files_written: usize,
    pub symbols_written: usize,
    pub imports_written: usize,
    pub calls_written: usize,
    pub chunks_written: usize,
    pub graph_sync_pending: bool,
    pub vectors_sync_pending: bool,
}

impl CodeFactWriteSummary {
    pub fn for_file(symbols: usize, imports: usize, calls: usize, chunks: usize) -> Self {
        Self {
            files_written: 1,
            symbols_written: symbols,
            imports_written: imports,
            calls_written: calls,
            chunks_written: chunks,
            graph_sync_pending: true,
            vectors_sync_pending: true,
        }
    }
}

pub fn delete_file_facts(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<()> {
    conn.execute(
        "DELETE FROM code_symbols WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    )?;
    delete_file_non_symbol_facts(conn, project_id, file_path)
}

pub fn delete_file_non_symbol_facts(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<()> {
    conn.execute(
        "DELETE FROM code_indexed_files WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    )?;
    conn.execute(
        "DELETE FROM code_content_chunks WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    )?;
    conn.execute(
        "DELETE FROM code_imports WHERE project_id = $1 AND source_file = $2",
        &[&project_id, &file_path],
    )?;
    conn.execute(
        "DELETE FROM code_calls WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    )?;
    Ok(())
}

pub fn delete_stale_file_symbols(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    current_symbol_ids: &[String],
) -> anyhow::Result<usize> {
    let deleted = if current_symbol_ids.is_empty() {
        conn.execute(
            "DELETE FROM code_symbols WHERE project_id = $1 AND file_path = $2",
            &[&project_id, &file_path],
        )?
    } else {
        let current_symbol_ids = current_symbol_ids.to_vec();
        conn.execute(
            "DELETE FROM code_symbols
             WHERE project_id = $1
               AND file_path = $2
               AND NOT (id = ANY($3::text[]))",
            &[&project_id, &file_path, &current_symbol_ids],
        )?
    };
    usize::try_from(deleted).map_err(|_| anyhow::anyhow!("deleted symbol count exceeds usize"))
}

pub fn file_facts_exist(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<bool> {
    let row = conn.query_one(
        "SELECT
            EXISTS(SELECT 1 FROM code_indexed_files WHERE project_id = $1 AND file_path = $2)
            OR EXISTS(SELECT 1 FROM code_symbols WHERE project_id = $1 AND file_path = $2)
            OR EXISTS(SELECT 1 FROM code_content_chunks WHERE project_id = $1 AND file_path = $2)
            OR EXISTS(SELECT 1 FROM code_imports WHERE project_id = $1 AND source_file = $2)
            OR EXISTS(SELECT 1 FROM code_calls WHERE project_id = $1 AND file_path = $2)",
        &[&project_id, &file_path],
    )?;
    Ok(row.try_get(0)?)
}

pub fn upsert_symbols(conn: &mut impl GenericClient, symbols: &[Symbol]) -> anyhow::Result<usize> {
    for chunk in symbols.chunks(SYMBOL_UPSERT_BATCH_SIZE) {
        let ids = chunk.iter().map(|sym| sym.id.clone()).collect::<Vec<_>>();
        let project_ids = chunk
            .iter()
            .map(|sym| sym.project_id.clone())
            .collect::<Vec<_>>();
        let file_paths = chunk
            .iter()
            .map(|sym| sym.file_path.clone())
            .collect::<Vec<_>>();
        let names = chunk.iter().map(|sym| sym.name.clone()).collect::<Vec<_>>();
        let qualified_names = chunk
            .iter()
            .map(|sym| sym.qualified_name.clone())
            .collect::<Vec<_>>();
        let kinds = chunk.iter().map(|sym| sym.kind.clone()).collect::<Vec<_>>();
        let languages = chunk
            .iter()
            .map(|sym| sym.language.clone())
            .collect::<Vec<_>>();
        let byte_starts = chunk
            .iter()
            .map(|sym| to_i32(sym.byte_start))
            .collect::<Vec<_>>();
        let byte_ends = chunk
            .iter()
            .map(|sym| to_i32(sym.byte_end))
            .collect::<Vec<_>>();
        let line_starts = chunk
            .iter()
            .map(|sym| to_i32(sym.line_start))
            .collect::<Vec<_>>();
        let line_ends = chunk
            .iter()
            .map(|sym| to_i32(sym.line_end))
            .collect::<Vec<_>>();
        let signatures = chunk
            .iter()
            .map(|sym| sym.signature.clone())
            .collect::<Vec<_>>();
        let docstrings = chunk
            .iter()
            .map(|sym| sym.docstring.clone())
            .collect::<Vec<_>>();
        let parent_symbol_ids = chunk
            .iter()
            .map(|sym| sym.parent_symbol_id.clone())
            .collect::<Vec<_>>();
        let content_hashes = chunk
            .iter()
            .map(|sym| sym.content_hash.clone())
            .collect::<Vec<_>>();
        let summaries = chunk
            .iter()
            .map(|sym| sym.summary.clone())
            .collect::<Vec<_>>();

        conn.execute(
            "INSERT INTO code_symbols (
                id, project_id, file_path, name, qualified_name,
                kind, language, byte_start, byte_end,
                line_start, line_end, signature, docstring,
                parent_symbol_id, content_hash, summary,
                created_at, updated_at
            )
            SELECT
                id, project_id, file_path, name, qualified_name,
                kind, language, byte_start, byte_end,
                line_start, line_end, signature, docstring,
                parent_symbol_id, content_hash, summary,
                NOW(), NOW()
            FROM unnest(
                $1::text[], $2::text[], $3::text[], $4::text[],
                $5::text[], $6::text[], $7::text[], $8::int4[],
                $9::int4[], $10::int4[], $11::int4[], $12::text[],
                $13::text[], $14::text[], $15::text[], $16::text[]
            ) AS t(
                id, project_id, file_path, name, qualified_name,
                kind, language, byte_start, byte_end,
                line_start, line_end, signature, docstring,
                parent_symbol_id, content_hash, summary
            )
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
                &ids,
                &project_ids,
                &file_paths,
                &names,
                &qualified_names,
                &kinds,
                &languages,
                &byte_starts,
                &byte_ends,
                &line_starts,
                &line_ends,
                &signatures,
                &docstrings,
                &parent_symbol_ids,
                &content_hashes,
                &summaries,
            ],
        )?;
    }
    Ok(symbols.len())
}

pub fn upsert_file(conn: &mut impl GenericClient, file: &IndexedFile) -> anyhow::Result<()> {
    conn.execute(
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
    )?;
    Ok(())
}

pub fn upsert_project_seed(
    conn: &mut impl GenericClient,
    project_id: &str,
    root_path: &std::path::Path,
) -> anyhow::Result<()> {
    let root_path = root_path.to_string_lossy().to_string();
    conn.execute(
        "INSERT INTO code_indexed_projects (
            id, root_path, total_files, total_symbols,
            last_indexed_at, index_duration_ms
        ) VALUES ($1,$2,0,0,NULL,0)
        ON CONFLICT(id) DO UPDATE SET
            root_path=excluded.root_path,
            updated_at=NOW()",
        &[&project_id, &root_path],
    )?;
    Ok(())
}

pub fn upsert_content_chunks(
    conn: &mut impl GenericClient,
    chunks: &[ContentChunk],
) -> anyhow::Result<usize> {
    for chunk in chunks {
        conn.execute(
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
        )?;
    }
    Ok(chunks.len())
}

pub fn upsert_project_stats(
    conn: &mut impl GenericClient,
    project: &IndexedProject,
) -> anyhow::Result<()> {
    conn.execute(
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
    )?;
    Ok(())
}

pub fn upsert_imports(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    imports: &[ImportRelation],
) -> anyhow::Result<usize> {
    conn.execute(
        "DELETE FROM code_imports WHERE project_id = $1 AND source_file = $2",
        &[&project_id, &file_path],
    )?;
    let mut rows_affected = 0usize;
    for imp in imports {
        rows_affected += conn.execute(
            "INSERT INTO code_imports (project_id, source_file, target_module)
             VALUES ($1, $2, $3)
             ON CONFLICT (project_id, source_file, target_module) DO NOTHING",
            &[&project_id, &imp.file_path, &imp.module_name],
        )? as usize;
    }
    Ok(rows_affected)
}

pub fn upsert_calls(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    calls: &[CallRelation],
) -> anyhow::Result<usize> {
    conn.execute(
        "DELETE FROM code_calls WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    )?;
    let mut rows_affected = 0usize;
    for call in calls {
        rows_affected += insert_call(conn, project_id, call)?;
    }
    Ok(rows_affected)
}

fn insert_call(
    conn: &mut impl GenericClient,
    project_id: &str,
    call: &CallRelation,
) -> anyhow::Result<usize> {
    let rows = conn.execute(
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
    )?;
    Ok(rows as usize)
}

/// Replace a pending `local_import` call row with its resolved form. The exact
/// pending row (identified by its persisted columns) is deleted and the
/// `resolved` call — a `Symbol` target on a hit, `Unresolved` on a miss — is
/// inserted in its place. Used by the post-write local-import resolution pass.
pub fn promote_local_import_call(
    conn: &mut impl GenericClient,
    project_id: &str,
    original: &CallRelation,
    resolved: &CallRelation,
) -> anyhow::Result<()> {
    conn.execute(
        "DELETE FROM code_calls
         WHERE project_id = $1 AND caller_symbol_id = $2 AND callee_symbol_id = ''
           AND callee_name = $3 AND callee_target_kind = 'local_import'
           AND callee_external_module = $4 AND file_path = $5 AND line = $6",
        &[
            &project_id,
            &original.caller_symbol_id,
            &original.callee_name,
            &original.callee_external_module.as_deref().unwrap_or(""),
            &original.file_path,
            &to_i32(original.line),
        ],
    )?;
    insert_call(conn, project_id, resolved)?;
    Ok(())
}

fn to_i32(value: usize) -> i32 {
    value.min(i32::MAX as usize) as i32
}

use postgres::GenericClient;
use serde::{Deserialize, Serialize};

pub use crate::index::indexer::{
    IndexDegradation, IndexDurations, IndexOptions, IndexOutcome, IndexProgressSink, IndexRequest,
    UnsupportedFileType, index_files, project_changed_since,
};

use crate::db::{id_param, id_params, opt_id_param};
use crate::models::{
    CallRelation, ContentChunk, ImportRelation, IndexedFile, IndexedProject, InheritanceRelation,
    Symbol,
};

const SYMBOL_UPSERT_BATCH_SIZE: usize = 500;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeFactWriteRequest {
    pub project_id: String,
    pub file_path: String,
    pub symbols: usize,
    pub imports: usize,
    pub calls: usize,
    pub inheritance: usize,
    pub chunks: usize,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeFactWriteSummary {
    pub files_written: usize,
    pub symbols_written: usize,
    pub imports_written: usize,
    pub calls_written: usize,
    pub inheritance_written: usize,
    pub chunks_written: usize,
    pub graph_sync_pending: bool,
    pub vectors_sync_pending: bool,
}

impl CodeFactWriteSummary {
    pub fn for_file(
        symbols: usize,
        imports: usize,
        calls: usize,
        inheritance: usize,
        chunks: usize,
    ) -> Self {
        Self {
            files_written: 1,
            symbols_written: symbols,
            imports_written: imports,
            calls_written: calls,
            inheritance_written: inheritance,
            chunks_written: chunks,
            graph_sync_pending: true,
            vectors_sync_pending: true,
        }
    }
}

pub fn delete_stale_file_symbols(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    content_hash: &str,
    current_symbol_ids: &[String],
) -> anyhow::Result<usize> {
    let project_id = id_param(project_id)?;
    let deleted = if current_symbol_ids.is_empty() {
        conn.execute(
            "DELETE FROM code_symbols
             WHERE project_id = $1 AND file_path = $2 AND file_content_hash = $3",
            &[&project_id, &file_path, &content_hash],
        )?
    } else {
        let current_symbol_ids = id_params(current_symbol_ids)?;
        conn.execute(
            "DELETE FROM code_symbols
             WHERE project_id = $1
               AND file_path = $2
               AND file_content_hash = $3
               AND NOT (id = ANY($4::uuid[]))",
            &[&project_id, &file_path, &content_hash, &current_symbol_ids],
        )?
    };
    usize::try_from(deleted).map_err(|_| anyhow::anyhow!("deleted symbol count exceeds usize"))
}

pub fn delete_content_version_non_symbol_facts(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    content_hash: &str,
) -> anyhow::Result<()> {
    let project_id = id_param(project_id)?;
    conn.execute(
        "DELETE FROM code_content_chunks
         WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
        &[&project_id, &file_path, &content_hash],
    )?;
    conn.execute(
        "DELETE FROM code_imports
         WHERE project_id = $1 AND source_file = $2 AND content_hash = $3",
        &[&project_id, &file_path, &content_hash],
    )?;
    conn.execute(
        "DELETE FROM code_calls
         WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
        &[&project_id, &file_path, &content_hash],
    )?;
    conn.execute(
        "DELETE FROM code_inheritance
         WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
        &[&project_id, &file_path, &content_hash],
    )?;
    Ok(())
}

pub fn upsert_symbols(conn: &mut impl GenericClient, symbols: &[Symbol]) -> anyhow::Result<usize> {
    for chunk in symbols.chunks(SYMBOL_UPSERT_BATCH_SIZE) {
        let ids = chunk
            .iter()
            .map(|sym| id_param(&sym.id))
            .collect::<anyhow::Result<Vec<_>>>()?;
        let project_ids = chunk
            .iter()
            .map(|sym| id_param(&sym.project_id))
            .collect::<anyhow::Result<Vec<_>>>()?;
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
            .map(|sym| opt_id_param(sym.parent_symbol_id.as_deref().unwrap_or("")))
            .collect::<anyhow::Result<Vec<_>>>()?;
        let file_content_hashes = chunk
            .iter()
            .map(|sym| sym.file_content_hash.clone())
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
                parent_symbol_id, file_content_hash, content_hash, summary,
                created_at, updated_at
            )
            SELECT
                id, project_id, file_path, name, qualified_name,
                kind, language, byte_start, byte_end,
                line_start, line_end, signature, docstring,
                parent_symbol_id, file_content_hash, content_hash, summary,
                NOW(), NOW()
            FROM unnest(
                $1::uuid[], $2::uuid[], $3::text[], $4::text[],
                $5::text[], $6::text[], $7::text[], $8::int4[],
                $9::int4[], $10::int4[], $11::int4[], $12::text[],
                $13::text[], $14::uuid[], $15::text[], $16::text[], $17::text[]
            ) AS t(
                id, project_id, file_path, name, qualified_name,
                kind, language, byte_start, byte_end,
                line_start, line_end, signature, docstring,
                parent_symbol_id, file_content_hash, content_hash, summary
            )
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, qualified_name=excluded.qualified_name,
                kind=excluded.kind, byte_start=excluded.byte_start,
                byte_end=excluded.byte_end, line_start=excluded.line_start,
                line_end=excluded.line_end, signature=excluded.signature,
                docstring=excluded.docstring, parent_symbol_id=excluded.parent_symbol_id,
                language=excluded.language, file_content_hash=excluded.file_content_hash,
                content_hash=excluded.content_hash,
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
                &file_content_hashes,
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
            graph_sync_attempted_at, vector_sync_attempted_at, indexed_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,false,false,NULL,NULL,NOW())
        ON CONFLICT(id) DO UPDATE SET
            language=excluded.language,
            symbol_count=excluded.symbol_count,
            byte_size=excluded.byte_size,
            indexed_at=NOW(),
            last_referenced_at=NOW()",
        &[
            &id_param(&file.id)?,
            &id_param(&file.project_id)?,
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
    machine_id: &str,
    project_id: &str,
    root_path: &std::path::Path,
) -> anyhow::Result<()> {
    let machine_id = id_param(machine_id)?;
    let project_id = id_param(project_id)?;
    let root_path = root_path.to_string_lossy().to_string();
    conn.execute(
        "INSERT INTO code_indexed_projects (id) VALUES ($1)
         ON CONFLICT(id) DO UPDATE SET updated_at=NOW()",
        &[&project_id],
    )?;
    conn.execute(
        "INSERT INTO code_indexed_project_states (
            machine_id, project_id, root_path, total_files, total_symbols,
            last_indexed_at, index_duration_ms
        ) VALUES ($1,$2,$3,0,0,NULL,0)
        ON CONFLICT(machine_id, project_id) DO UPDATE SET
            root_path=excluded.root_path,
            updated_at=NOW()",
        &[&machine_id, &project_id, &root_path],
    )?;
    Ok(())
}

pub fn upsert_file_state(
    conn: &mut impl GenericClient,
    machine_id: &str,
    file: &IndexedFile,
) -> anyhow::Result<()> {
    conn.execute(
        "WITH state AS (
            INSERT INTO code_indexed_file_states (
                machine_id, project_id, file_path, content_hash
            ) VALUES ($1,$2,$3,$4)
            ON CONFLICT(machine_id, project_id, file_path) DO UPDATE SET
                content_hash=excluded.content_hash,
                updated_at=NOW()
            RETURNING project_id, file_path, content_hash
         )
         UPDATE code_indexed_files f
            SET last_referenced_at = NOW()
           FROM state s
          WHERE f.project_id = s.project_id
            AND f.file_path = s.file_path
            AND f.content_hash = s.content_hash",
        &[
            &id_param(machine_id)?,
            &id_param(&file.project_id)?,
            &file.file_path,
            &file.content_hash,
        ],
    )?;
    Ok(())
}

pub fn adopt_file_state(
    conn: &mut impl GenericClient,
    machine_id: &str,
    project_id: &str,
    file_path: &str,
    content_hash: &str,
) -> anyhow::Result<bool> {
    let machine_id = id_param(machine_id)?;
    let project_id = id_param(project_id)?;
    let adopted = conn.execute(
        "WITH adopted AS (
            INSERT INTO code_indexed_file_states (
                machine_id, project_id, file_path, content_hash
            )
            SELECT $1, project_id, file_path, content_hash
            FROM code_indexed_files
            WHERE project_id = $2
              AND file_path = $3
              AND content_hash = $4
              AND graph_synced
              AND vectors_synced
            ON CONFLICT(machine_id, project_id, file_path) DO UPDATE SET
                content_hash=excluded.content_hash,
                updated_at=NOW()
            RETURNING project_id, file_path, content_hash
         )
         UPDATE code_indexed_files f
            SET last_referenced_at = NOW()
           FROM adopted a
          WHERE f.project_id = a.project_id
            AND f.file_path = a.file_path
            AND f.content_hash = a.content_hash",
        &[&machine_id, &project_id, &file_path, &content_hash],
    )?;
    Ok(adopted > 0)
}

pub fn delete_file_state(
    conn: &mut impl GenericClient,
    machine_id: &str,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<bool> {
    let deleted = conn.execute(
        "DELETE FROM code_indexed_file_states
         WHERE machine_id = $1 AND project_id = $2 AND file_path = $3",
        &[&id_param(machine_id)?, &id_param(project_id)?, &file_path],
    )?;
    Ok(deleted > 0)
}

pub fn upsert_content_chunks(
    conn: &mut impl GenericClient,
    chunks: &[ContentChunk],
) -> anyhow::Result<usize> {
    for chunk in chunks {
        conn.execute(
            "INSERT INTO code_content_chunks (
                id, project_id, file_path, content_hash, chunk_index,
                line_start, line_end, content, language, created_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,NOW())
            ON CONFLICT(id) DO UPDATE SET
                content=excluded.content,
                line_start=excluded.line_start,
                line_end=excluded.line_end",
            &[
                &id_param(&chunk.id)?,
                &id_param(&chunk.project_id)?,
                &chunk.file_path,
                &chunk.content_hash,
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
    machine_id: &str,
    project: &IndexedProject,
) -> anyhow::Result<()> {
    let project_id = id_param(&project.id)?;
    conn.execute(
        "INSERT INTO code_indexed_projects (id) VALUES ($1)
         ON CONFLICT(id) DO UPDATE SET updated_at=NOW()",
        &[&project_id],
    )?;
    conn.execute(
        "INSERT INTO code_indexed_project_states (
            machine_id, project_id, root_path, total_files, total_symbols,
            last_indexed_at, index_duration_ms
        ) VALUES ($1,$2,$3,$4,$5,NOW(),$6)
        ON CONFLICT(machine_id, project_id) DO UPDATE SET
            root_path=excluded.root_path,
            total_files=excluded.total_files,
            total_symbols=excluded.total_symbols,
            last_indexed_at=excluded.last_indexed_at,
            index_duration_ms=excluded.index_duration_ms,
            updated_at=NOW()",
        &[
            &id_param(machine_id)?,
            &project_id,
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
    content_hash: &str,
    imports: &[ImportRelation],
) -> anyhow::Result<usize> {
    let project_id = id_param(project_id)?;
    conn.execute(
        "DELETE FROM code_imports
         WHERE project_id = $1 AND source_file = $2 AND content_hash = $3",
        &[&project_id, &file_path, &content_hash],
    )?;
    let mut rows_affected = 0usize;
    for imp in imports {
        rows_affected += conn.execute(
            "INSERT INTO code_imports (project_id, source_file, content_hash, target_module)
             VALUES ($1, $2, $3, $4)
             ON CONFLICT (project_id, source_file, content_hash, target_module) DO NOTHING",
            &[&project_id, &imp.file_path, &content_hash, &imp.module_name],
        )? as usize;
    }
    Ok(rows_affected)
}

pub fn upsert_calls(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    content_hash: &str,
    calls: &[CallRelation],
) -> anyhow::Result<usize> {
    let project_uuid = id_param(project_id)?;
    conn.execute(
        "DELETE FROM code_calls
         WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
        &[&project_uuid, &file_path, &content_hash],
    )?;
    let mut rows_affected = 0usize;
    for call in calls {
        rows_affected += insert_call(conn, project_id, content_hash, call)?;
    }
    Ok(rows_affected)
}

fn insert_call(
    conn: &mut impl GenericClient,
    project_id: &str,
    content_hash: &str,
    call: &CallRelation,
) -> anyhow::Result<usize> {
    let project_id = id_param(project_id)?;
    // The domain "" sentinel (module-scope caller, absent callee) becomes NULL
    // in the nullable uuid columns; `code_calls_unique_call_target` is declared
    // NULLS NOT DISTINCT, so ON CONFLICT dedup still applies to NULL targets.
    let caller_symbol_id = opt_id_param(&call.caller_symbol_id)?;
    let callee_symbol_id = opt_id_param(call.callee_symbol_id.as_deref().unwrap_or(""))?;
    let rows = conn.execute(
        "INSERT INTO code_calls
         (project_id, caller_symbol_id, callee_symbol_id, callee_name, \
          callee_target_kind, callee_external_module, file_path, content_hash, line)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
         ON CONFLICT (
            project_id, file_path, content_hash, caller_symbol_id, callee_symbol_id,
            callee_name, callee_target_kind, callee_external_module, line
         ) DO NOTHING",
        &[
            &project_id,
            &caller_symbol_id,
            &callee_symbol_id,
            &call.callee_name,
            &call.callee_target_kind.as_str(),
            &call.callee_external_module.as_deref().unwrap_or(""),
            &call.file_path,
            &content_hash,
            &to_i32(call.line),
        ],
    )?;
    Ok(rows as usize)
}

pub fn upsert_inheritance(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    content_hash: &str,
    inheritance: &[InheritanceRelation],
) -> anyhow::Result<usize> {
    let project_uuid = id_param(project_id)?;
    conn.execute(
        "DELETE FROM code_inheritance
         WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
        &[&project_uuid, &file_path, &content_hash],
    )?;
    let mut rows_affected = 0usize;
    for relation in inheritance {
        rows_affected += insert_inheritance(conn, project_id, content_hash, relation)?;
    }
    Ok(rows_affected)
}

fn insert_inheritance(
    conn: &mut impl GenericClient,
    project_id: &str,
    content_hash: &str,
    relation: &InheritanceRelation,
) -> anyhow::Result<usize> {
    let project_id = id_param(project_id)?;
    let source_symbol_id = opt_id_param(relation.source_symbol_id.as_deref().unwrap_or(""))?;
    let target_symbol_id = opt_id_param(relation.target_symbol_id.as_deref().unwrap_or(""))?;
    let rows = conn.execute(
        "INSERT INTO code_inheritance
         (project_id, source_symbol_id, source_name, source_kind, source_external_module,
          target_symbol_id, target_name, target_kind, target_external_module,
          heritage_kind, file_path, content_hash, line)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
         ON CONFLICT (
            project_id, file_path, content_hash,
            source_symbol_id, source_name, source_kind, source_external_module,
            target_symbol_id, target_name, target_kind, target_external_module,
            heritage_kind, line
         ) DO NOTHING",
        &[
            &project_id,
            &source_symbol_id,
            &relation.source_name,
            &relation.source_kind.as_str(),
            &relation.source_external_module.as_deref().unwrap_or(""),
            &target_symbol_id,
            &relation.target_name,
            &relation.target_kind.as_str(),
            &relation.target_external_module.as_deref().unwrap_or(""),
            &relation.heritage_kind.as_rel_type(),
            &relation.file_path,
            &content_hash,
            &to_i32(relation.line),
        ],
    )?;
    Ok(rows as usize)
}

/// Replace a pending inheritance row with an independently promoted form.
/// A source hit clears only `source_external_module`; a target hit clears only
/// `target_external_module`. A miss is not rewritten here.
pub fn promote_inheritance_row(
    conn: &mut impl GenericClient,
    project_id: &str,
    original: &InheritanceRelation,
    resolved: &InheritanceRelation,
) -> anyhow::Result<()> {
    let project_uuid = id_param(project_id)?;
    let source_symbol_id = opt_id_param(original.source_symbol_id.as_deref().unwrap_or(""))?;
    let target_symbol_id = opt_id_param(original.target_symbol_id.as_deref().unwrap_or(""))?;
    conn.execute(
        "DELETE FROM code_inheritance
         WHERE project_id = $1
           AND file_path = $2
           AND content_hash = $3
           AND source_symbol_id IS NOT DISTINCT FROM $4
           AND source_name = $5
           AND source_kind = $6
           AND source_external_module = $7
           AND target_symbol_id IS NOT DISTINCT FROM $8
           AND target_name = $9
           AND target_kind = $10
           AND target_external_module = $11
           AND heritage_kind = $12
           AND line = $13",
        &[
            &project_uuid,
            &original.file_path,
            &original.content_hash,
            &source_symbol_id,
            &original.source_name,
            &original.source_kind.as_str(),
            &original.source_external_module.as_deref().unwrap_or(""),
            &target_symbol_id,
            &original.target_name,
            &original.target_kind.as_str(),
            &original.target_external_module.as_deref().unwrap_or(""),
            &original.heritage_kind.as_rel_type(),
            &to_i32(original.line),
        ],
    )?;
    insert_inheritance(conn, project_id, &original.content_hash, resolved)?;
    Ok(())
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
    let project_uuid = id_param(project_id)?;
    // Pending local_import rows never carry a callee, and NULL caller means
    // module scope, so both uuid predicates are NULL-aware.
    let caller_symbol_id = opt_id_param(&original.caller_symbol_id)?;
    conn.execute(
        "DELETE FROM code_calls
         WHERE project_id = $1
           AND caller_symbol_id IS NOT DISTINCT FROM $2
            AND callee_symbol_id IS NULL
            AND callee_name = $3 AND callee_target_kind = 'local_import'
            AND callee_external_module = $4 AND file_path = $5
            AND content_hash = $6 AND line = $7",
        &[
            &project_uuid,
            &caller_symbol_id,
            &original.callee_name,
            &original.callee_external_module.as_deref().unwrap_or(""),
            &original.file_path,
            &original.content_hash,
            &to_i32(original.line),
        ],
    )?;
    insert_call(conn, project_id, &original.content_hash, resolved)?;
    Ok(())
}

fn to_i32(value: usize) -> i32 {
    value.min(i32::MAX as usize) as i32
}

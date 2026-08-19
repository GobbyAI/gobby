use std::time::SystemTime;

use anyhow::bail;
use postgres::GenericClient;

use super::ids::{id_param, id_string, opt_id_string};
use crate::models::{
    CallRelation, CallTargetKind, HeritageKind, ImportRelation, InheritanceRelation, Symbol,
};
use crate::utils::i64_to_usize;

#[derive(Debug, Clone)]
pub struct GraphSyncAttempt {
    pub content_hash: String,
    pub attempted_at: SystemTime,
}

#[derive(Debug, Clone)]
pub struct GraphFileFacts {
    pub file_path: String,
    pub content_hash: String,
    pub imports: Vec<ImportRelation>,
    pub definitions: Vec<Symbol>,
    pub calls: Vec<CallRelation>,
    pub inheritance: Vec<InheritanceRelation>,
}

pub fn list_indexed_file_paths(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<Vec<String>> {
    let machine_id = id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let project_id = id_param(project_id)?;
    let rows = conn.query(
        "SELECT file_path
         FROM code_indexed_file_states
         WHERE machine_id = $1 AND project_id = $2
         ORDER BY file_path",
        &[&machine_id, &project_id],
    )?;
    rows.into_iter()
        .map(|row| row.try_get("file_path").map_err(Into::into))
        .collect()
}

/// File paths referenced by any machine's file-state for the project.
///
/// Projection orphan cleanup must compute its keep-set from this listing: a
/// shared projection row survives while any machine's file-state references
/// its path, so a machine-scoped listing would delete other machines' live
/// projection data.
pub fn list_all_machine_indexed_file_paths(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<Vec<String>> {
    let project_id = id_param(project_id)?;
    let rows = conn.query(
        "SELECT DISTINCT file_path
         FROM code_indexed_file_states
         WHERE project_id = $1
         ORDER BY file_path",
        &[&project_id],
    )?;
    rows.into_iter()
        .map(|row| row.try_get("file_path").map_err(Into::into))
        .collect()
}

pub fn indexed_project_exists(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<bool> {
    let project_id = id_param(project_id)?;
    Ok(conn
        .query_opt(
            "SELECT 1 FROM code_indexed_projects WHERE id = $1",
            &[&project_id],
        )?
        .is_some())
}

pub fn read_graph_file_facts(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<GraphFileFacts> {
    let machine_id = id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let project_uuid = id_param(project_id)?;
    let content_hash: String = conn
        .query_one(
            "SELECT content_hash
             FROM code_indexed_file_states
             WHERE machine_id = $1 AND project_id = $2 AND file_path = $3",
            &[&machine_id, &project_uuid, &file_path],
        )?
        .try_get("content_hash")?;
    let imports = read_imports_for_file(conn, project_id, file_path, &content_hash)?;
    let definitions = read_symbols_for_file(conn, project_id, file_path, &content_hash)?;
    let calls = read_calls_for_file(conn, project_id, file_path, &content_hash)?;
    let inheritance = read_inheritance_for_file(conn, project_id, file_path, &content_hash)?;

    Ok(GraphFileFacts {
        file_path: file_path.to_string(),
        content_hash,
        imports,
        definitions,
        calls,
        inheritance,
    })
}

pub fn mark_graph_sync_attempted(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<Option<GraphSyncAttempt>> {
    let machine_id = id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let project_id = id_param(project_id)?;
    let row = conn.query_opt(
        "UPDATE code_indexed_files cif
         SET graph_synced = false, graph_sync_attempted_at = NOW()
         FROM code_indexed_file_states cifs
         WHERE cifs.machine_id = $1
           AND cifs.project_id = $2 AND cifs.file_path = $3
           AND cif.project_id = cifs.project_id
           AND cif.file_path = cifs.file_path
           AND cif.content_hash = cifs.content_hash
         RETURNING cif.content_hash, cif.graph_sync_attempted_at",
        &[&machine_id, &project_id, &file_path],
    )?;
    row.map(|row| {
        Ok(GraphSyncAttempt {
            content_hash: row.try_get("content_hash")?,
            attempted_at: row.try_get("graph_sync_attempted_at")?,
        })
    })
    .transpose()
}

pub fn mark_graph_synced(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    content_hash: &str,
    attempted_at: SystemTime,
) -> anyhow::Result<bool> {
    let machine_id = id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let project_id = id_param(project_id)?;
    let succeeded: bool = conn
        .query_one(
            "WITH cas AS (
                UPDATE code_indexed_files cif
                SET graph_synced = true
                FROM code_indexed_file_states cifs
                WHERE cifs.machine_id = $1
                  AND cifs.project_id = $2 AND cifs.file_path = $3
                  AND cif.project_id = cifs.project_id
                  AND cif.file_path = cifs.file_path
                  AND cif.content_hash = cifs.content_hash
                  AND cif.content_hash = $4
                  AND cif.graph_sync_attempted_at IS NOT DISTINCT FROM $5
                RETURNING cif.file_path
             ), dirty AS (
                UPDATE code_indexed_files cif
                SET graph_synced = false, graph_sync_attempted_at = NULL
                FROM code_indexed_file_states cifs
                WHERE NOT EXISTS (SELECT 1 FROM cas)
                  AND cifs.machine_id = $1
                  AND cifs.project_id = $2 AND cifs.file_path = $3
                  AND cif.project_id = cifs.project_id
                  AND cif.file_path = cifs.file_path
                  AND cif.content_hash = cifs.content_hash
                RETURNING cif.file_path
             )
             SELECT EXISTS (SELECT 1 FROM cas) AS succeeded",
            &[
                &machine_id,
                &project_id,
                &file_path,
                &content_hash,
                &attempted_at,
            ],
        )?
        .try_get("succeeded")?;
    Ok(succeeded)
}

/// Clear graph-sync completion on the active content row without stamping a
/// cooloff. Promotion uses this so pending recovery can project the new typed
/// edge; do not call [`mark_graph_sync_attempted`] for that dirty.
pub fn dirty_graph_sync_for_file(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    content_hash: &str,
) -> anyhow::Result<()> {
    let project_id = id_param(project_id)?;
    conn.execute(
        "UPDATE code_indexed_files cif
         SET graph_synced = false, graph_sync_attempted_at = NULL
         FROM code_indexed_file_states cifs
         WHERE cifs.project_id = $1
           AND cifs.file_path = $2
           AND cifs.content_hash = $3
           AND cif.project_id = cifs.project_id
           AND cif.file_path = cifs.file_path
           AND cif.content_hash = cifs.content_hash",
        &[&project_id, &file_path, &content_hash],
    )?;
    Ok(())
}

/// Reset graph sync flags for every machine's referenced content rows.
///
/// `graph clear`/`graph rebuild` wipe the shared per-project projection, so
/// rows referenced only by other machines must also drop to unsynced or those
/// machines would never resync into the emptied projection.
pub fn reset_graph_sync_for_project(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<u64> {
    let project_id = id_param(project_id)?;
    Ok(conn.execute(
        "UPDATE code_indexed_files cif
         SET graph_synced = false, graph_sync_attempted_at = NULL
         FROM code_indexed_file_states cifs
         WHERE cifs.project_id = $1
           AND cif.project_id = cifs.project_id
           AND cif.file_path = cifs.file_path
           AND cif.content_hash = cifs.content_hash",
        &[&project_id],
    )?)
}

pub fn mark_vector_sync_attempted(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<bool> {
    let machine_id = id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let project_id = id_param(project_id)?;
    let updated = conn.execute(
        "UPDATE code_indexed_files cif
         SET vectors_synced = false, vector_sync_attempted_at = NOW()
         FROM code_indexed_file_states cifs
         WHERE cifs.machine_id = $1
           AND cifs.project_id = $2 AND cifs.file_path = $3
           AND cif.project_id = cifs.project_id
           AND cif.file_path = cifs.file_path
           AND cif.content_hash = cifs.content_hash",
        &[&machine_id, &project_id, &file_path],
    )?;
    Ok(updated > 0)
}

pub fn mark_vectors_synced(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<bool> {
    let machine_id = id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let project_id = id_param(project_id)?;
    let updated = conn.execute(
        "UPDATE code_indexed_files cif
         SET vectors_synced = true, vector_sync_attempted_at = NOW()
         FROM code_indexed_file_states cifs
         WHERE cifs.machine_id = $1
           AND cifs.project_id = $2 AND cifs.file_path = $3
           AND cif.project_id = cifs.project_id
           AND cif.file_path = cifs.file_path
           AND cif.content_hash = cifs.content_hash",
        &[&machine_id, &project_id, &file_path],
    )?;
    Ok(updated > 0)
}

pub fn mark_project_vector_sync_attempted(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<u64> {
    let machine_id = id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let project_id = id_param(project_id)?;
    Ok(conn.execute(
        "UPDATE code_indexed_files cif
         SET vectors_synced = false, vector_sync_attempted_at = NOW()
         FROM code_indexed_file_states cifs
         WHERE cifs.machine_id = $1 AND cifs.project_id = $2
           AND cif.project_id = cifs.project_id
           AND cif.file_path = cifs.file_path
           AND cif.content_hash = cifs.content_hash",
        &[&machine_id, &project_id],
    )?)
}

pub fn mark_project_vectors_synced(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<u64> {
    let machine_id = id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let project_id = id_param(project_id)?;
    Ok(conn.execute(
        "UPDATE code_indexed_files cif
         SET vectors_synced = true, vector_sync_attempted_at = NOW()
         FROM code_indexed_file_states cifs
         WHERE cifs.machine_id = $1 AND cifs.project_id = $2
           AND cif.project_id = cifs.project_id
           AND cif.file_path = cifs.file_path
           AND cif.content_hash = cifs.content_hash",
        &[&machine_id, &project_id],
    )?)
}

/// Reset vector sync flags for every machine's referenced content rows.
///
/// `vector clear`/`vector rebuild` wipe the shared per-project Qdrant
/// collection, so rows referenced only by other machines must also drop to
/// unsynced or those machines would never resync into the emptied collection.
pub fn reset_vectors_sync_for_project(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<u64> {
    let project_id = id_param(project_id)?;
    Ok(conn.execute(
        "UPDATE code_indexed_files cif
         SET vectors_synced = false, vector_sync_attempted_at = NULL
         FROM code_indexed_file_states cifs
         WHERE cifs.project_id = $1
           AND cif.project_id = cifs.project_id
           AND cif.file_path = cifs.file_path
           AND cif.content_hash = cifs.content_hash",
        &[&project_id],
    )?)
}

/// Active visible `(source_file, target_module)` pairs for this machine's
/// current content hashes. MCG seed equivalence (plan 3.3) unions these with
/// path-derived aliases; do not persist a provider file on IMPORTS edges.
#[cfg_attr(
    not(test),
    expect(
        dead_code,
        reason = "MCG seed equivalence (plan 3.3) is the first production caller"
    )
)]
pub fn read_active_imports(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<Vec<ImportRelation>> {
    let machine_id = id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let project_id = id_param(project_id)?;
    let rows = conn.query(
        "SELECT ci.source_file, ci.target_module
         FROM code_imports ci
         JOIN code_indexed_file_states cifs
           ON cifs.project_id = ci.project_id
          AND cifs.file_path = ci.source_file
          AND cifs.content_hash = ci.content_hash
          AND cifs.machine_id = $1
         WHERE ci.project_id = $2
         ORDER BY ci.source_file, ci.target_module",
        &[&machine_id, &project_id],
    )?;
    rows.into_iter()
        .map(|row| {
            Ok(ImportRelation {
                file_path: row.try_get("source_file")?,
                module_name: row.try_get("target_module")?,
            })
        })
        .collect()
}

fn read_imports_for_file(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    content_hash: &str,
) -> anyhow::Result<Vec<ImportRelation>> {
    let project_id = id_param(project_id)?;
    let rows = conn.query(
        "SELECT source_file, target_module
         FROM code_imports
         WHERE project_id = $1 AND source_file = $2 AND content_hash = $3
         ORDER BY target_module",
        &[&project_id, &file_path, &content_hash],
    )?;
    rows.into_iter()
        .map(|row| {
            Ok(ImportRelation {
                file_path: row.try_get("source_file")?,
                module_name: row.try_get("target_module")?,
            })
        })
        .collect()
}

fn read_symbols_for_file(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    content_hash: &str,
) -> anyhow::Result<Vec<Symbol>> {
    let project_id = id_param(project_id)?;
    let query = format!(
        "SELECT {} FROM code_symbols s
         WHERE s.project_id = $1 AND s.file_path = $2 AND s.file_content_hash = $3
         ORDER BY s.line_start, s.byte_start",
        symbol_select_columns("s")
    );
    let rows = conn.query(&query, &[&project_id, &file_path, &content_hash])?;
    rows.iter().map(Symbol::from_row).collect()
}

fn read_inheritance_for_file(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    content_hash: &str,
) -> anyhow::Result<Vec<InheritanceRelation>> {
    let project_id = id_param(project_id)?;
    let rows = conn.query(
        &format!(
            "SELECT {INHERITANCE_SELECT}
             FROM code_inheritance ci
             WHERE ci.project_id = $1 AND ci.file_path = $2 AND ci.content_hash = $3
             ORDER BY ci.line, ci.source_name, ci.target_name"
        ),
        &[&project_id, &file_path, &content_hash],
    )?;
    rows.iter().map(inheritance_relation_from_row).collect()
}

fn read_calls_for_file(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    content_hash: &str,
) -> anyhow::Result<Vec<CallRelation>> {
    let project_id = id_param(project_id)?;
    let rows = conn.query(
        "SELECT caller_symbol_id, callee_symbol_id, callee_name,
                callee_target_kind, callee_external_module, file_path, content_hash,
                line::BIGINT AS line
         FROM code_calls
         WHERE project_id = $1 AND file_path = $2 AND content_hash = $3
         ORDER BY line, caller_symbol_id, callee_name",
        &[&project_id, &file_path, &content_hash],
    )?;
    rows.iter().map(call_relation_from_row).collect()
}

fn call_relation_from_row(row: &postgres::Row) -> anyhow::Result<CallRelation> {
    let target_kind: String = row.try_get("callee_target_kind")?;
    let callee_external_module: String = row.try_get("callee_external_module")?;
    Ok(CallRelation {
        // NULL caller means module scope; the domain keeps the "" sentinel.
        caller_symbol_id: opt_id_string(row, "caller_symbol_id")?.unwrap_or_default(),
        callee_symbol_id: opt_id_string(row, "callee_symbol_id")?,
        callee_name: row.try_get("callee_name")?,
        callee_target_kind: call_target_kind_from_str(&target_kind)?,
        callee_external_module: non_empty(callee_external_module),
        file_path: row.try_get("file_path")?,
        content_hash: row.try_get("content_hash")?,
        line: i64_to_usize(row.try_get("line")?, "line")?,
    })
}

/// Read the pending `local_import` calls written for `file_paths` during the
/// current index run. Each returned `CallRelation` carries its candidate target
/// files in `callee_external_module` (see `CallRelation::with_local_import_target`).
pub fn read_local_import_calls(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_paths: &[String],
) -> anyhow::Result<Vec<CallRelation>> {
    if file_paths.is_empty() {
        return Ok(Vec::new());
    }
    let project_id = id_param(project_id)?;
    let rows = conn.query(
        "SELECT caller_symbol_id, callee_symbol_id, callee_name,
                callee_target_kind, callee_external_module, file_path, content_hash,
                line::BIGINT AS line
         FROM code_calls
         WHERE project_id = $1 AND file_path = ANY($2)
           AND callee_target_kind = 'local_import'
         ORDER BY file_path, line, caller_symbol_id, callee_name",
        &[&project_id, &file_paths],
    )?;
    rows.iter().map(call_relation_from_row).collect()
}

pub fn read_project_local_import_calls(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<Vec<CallRelation>> {
    let project_id = id_param(project_id)?;
    let rows = conn.query(
        "SELECT caller_symbol_id, callee_symbol_id, callee_name,
                callee_target_kind, callee_external_module, file_path, content_hash,
                line::BIGINT AS line
         FROM code_calls
         WHERE project_id = $1 AND callee_target_kind = 'local_import'
         ORDER BY file_path, line, caller_symbol_id, callee_name",
        &[&project_id],
    )?;
    rows.iter().map(call_relation_from_row).collect()
}

const INHERITANCE_SELECT: &str = "ci.source_symbol_id, ci.source_name, ci.source_kind,
                ci.source_external_module, ci.target_symbol_id, ci.target_name,
                ci.target_kind, ci.target_external_module, ci.heritage_kind,
                ci.file_path, ci.content_hash, ci.line::BIGINT AS line";

fn inheritance_relation_from_row(row: &postgres::Row) -> anyhow::Result<InheritanceRelation> {
    let source_kind: String = row.try_get("source_kind")?;
    let target_kind: String = row.try_get("target_kind")?;
    let source_external_module: String = row.try_get("source_external_module")?;
    let target_external_module: String = row.try_get("target_external_module")?;
    let heritage_kind: String = row.try_get("heritage_kind")?;
    Ok(InheritanceRelation {
        source_symbol_id: opt_id_string(row, "source_symbol_id")?,
        source_name: row.try_get("source_name")?,
        source_kind: call_target_kind_from_str(&source_kind)?,
        source_external_module: non_empty(source_external_module),
        target_symbol_id: opt_id_string(row, "target_symbol_id")?,
        target_name: row.try_get("target_name")?,
        target_kind: call_target_kind_from_str(&target_kind)?,
        target_external_module: non_empty(target_external_module),
        heritage_kind: heritage_kind_from_str(&heritage_kind)?,
        file_path: row.try_get("file_path")?,
        content_hash: row.try_get("content_hash")?,
        line: i64_to_usize(row.try_get("line")?, "line")?,
    })
}

/// Pending LocalImport inheritance rows owned by `file_paths` on this machine's
/// active content hash. Inactive retained rows are excluded.
pub fn read_local_import_inheritance(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_paths: &[String],
) -> anyhow::Result<Vec<InheritanceRelation>> {
    if file_paths.is_empty() {
        return Ok(Vec::new());
    }
    let machine_id = id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let project_id = id_param(project_id)?;
    let rows = conn.query(
        &format!(
            "SELECT {INHERITANCE_SELECT}
             FROM code_inheritance ci
             JOIN code_indexed_file_states cifs
               ON cifs.project_id = ci.project_id
              AND cifs.file_path = ci.file_path
              AND cifs.content_hash = ci.content_hash
              AND cifs.machine_id = $1
             WHERE ci.project_id = $2 AND ci.file_path = ANY($3)
               AND (ci.source_kind = 'local_import' OR ci.target_kind = 'local_import')
             ORDER BY ci.file_path, ci.line, ci.source_name, ci.target_name"
        ),
        &[&machine_id, &project_id, &file_paths],
    )?;
    rows.iter().map(inheritance_relation_from_row).collect()
}

/// Project-wide pending LocalImport inheritance rows on this machine's active
/// content. Used for provider-later recovery and full-index sweeps.
pub fn read_project_local_import_inheritance(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<Vec<InheritanceRelation>> {
    let machine_id = id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let project_id = id_param(project_id)?;
    let rows = conn.query(
        &format!(
            "SELECT {INHERITANCE_SELECT}
             FROM code_inheritance ci
             JOIN code_indexed_file_states cifs
               ON cifs.project_id = ci.project_id
              AND cifs.file_path = ci.file_path
              AND cifs.content_hash = ci.content_hash
              AND cifs.machine_id = $1
             WHERE ci.project_id = $2
               AND (ci.source_kind = 'local_import' OR ci.target_kind = 'local_import')
             ORDER BY ci.file_path, ci.line, ci.source_name, ci.target_name"
        ),
        &[&machine_id, &project_id],
    )?;
    rows.iter().map(inheritance_relation_from_row).collect()
}

/// Resolve a cross-file local-import call target to its canonical `code_symbols`
/// id by `(candidate files, original name)`. Returns the real indexed id (no
/// UUID recompute, so a phantom edge is structurally impossible), or `None` when
/// nothing matches or the match is ambiguous.
///
/// Preference tiers, highest first:
/// 1. top-level (`parent_symbol_id IS NULL`) `function`/`class`
/// 2. `method`
/// 3. module-scoped `function` (Elixir `def` inside `defmodule`)
/// 4. top-level `type`
///
/// The best non-empty tier must contain exactly one symbol; otherwise the call
/// degrades to unresolved rather than risk a wrong edge.
pub fn resolve_local_callee_symbol_id(
    conn: &mut impl GenericClient,
    project_id: &str,
    target_files: &[String],
    name: &str,
) -> anyhow::Result<Option<String>> {
    if target_files.is_empty() || name.is_empty() {
        return Ok(None);
    }
    let machine_id = id_param(&gobby_core::machine::read_local_machine_id()?)?;
    let project_id = id_param(project_id)?;
    let rows = conn.query(
        "SELECT s.id, s.kind, s.parent_symbol_id
         FROM code_symbols s
         JOIN code_indexed_file_states cifs
           ON cifs.project_id = s.project_id
          AND cifs.file_path = s.file_path
          AND cifs.content_hash = s.file_content_hash
          AND cifs.machine_id = $1
         WHERE s.project_id = $2 AND s.file_path = ANY($3) AND s.name = $4
         ORDER BY s.file_path, s.byte_start",
        &[&machine_id, &project_id, &target_files, &name],
    )?;

    let candidates: Vec<LocalCalleeCandidate> = rows
        .iter()
        .map(|row| {
            let id = id_string(row, "id")?;
            let kind: String = row.try_get("kind")?;
            let parent_symbol_id = opt_id_string(row, "parent_symbol_id")?;
            Ok::<_, anyhow::Error>(LocalCalleeCandidate {
                id,
                kind,
                parent_symbol_id,
            })
        })
        .collect::<Result<_, _>>()?;

    Ok(select_local_callee_candidate_id(&candidates))
}

pub fn resolve_default_import_symbol_id(
    conn: &mut impl GenericClient,
    project_id: &str,
    target_files: &[String],
) -> anyhow::Result<Option<String>> {
    if target_files.is_empty() {
        return Ok(None);
    }
    let project_id = id_param(project_id)?;
    let target_kinds = ["function", "class", "type"];
    let rows = conn.query(
        "SELECT id, kind, parent_symbol_id
         FROM code_symbols
         WHERE project_id = $1 AND file_path = ANY($2)
           AND parent_symbol_id IS NULL
           AND kind = ANY($3)
         ORDER BY file_path, byte_start",
        &[&project_id, &target_files, &target_kinds.as_slice()],
    )?;

    let candidates: Vec<LocalCalleeCandidate> = rows
        .iter()
        .map(|row| {
            let id = id_string(row, "id")?;
            let kind: String = row.try_get("kind")?;
            let parent_symbol_id = opt_id_string(row, "parent_symbol_id")?;
            Ok::<_, anyhow::Error>(LocalCalleeCandidate {
                id,
                kind,
                parent_symbol_id,
            })
        })
        .collect::<Result<_, _>>()?;

    Ok(select_default_import_candidate_id(&candidates))
}

#[derive(Debug)]
struct LocalCalleeCandidate {
    id: String,
    kind: String,
    parent_symbol_id: Option<String>,
}

fn select_local_callee_candidate_id(candidates: &[LocalCalleeCandidate]) -> Option<String> {
    let top_level: Vec<&String> = candidates
        .iter()
        .filter(|candidate| {
            candidate.parent_symbol_id.is_none()
                && matches!(candidate.kind.as_str(), "function" | "class")
        })
        .map(|candidate| &candidate.id)
        .collect();
    if !top_level.is_empty() {
        return unique_id(&top_level);
    }

    let methods: Vec<&String> = candidates
        .iter()
        .filter(|candidate| candidate.kind == "method")
        .map(|candidate| &candidate.id)
        .collect();
    if !methods.is_empty() {
        return unique_id(&methods);
    }

    // Elixir `def greet(name)` remains a function under its defmodule parent.
    // Non-Elixir nested functions are normalized to method in parser::link_parents,
    // so this tier only catches module-scoped Elixir functions. Multi-clause or
    // multi-arity defs still produce multiple same-name rows; the unique guard
    // keeps those ambiguous calls unresolved until resolution tracks arity.
    let module_scoped_functions: Vec<&String> = candidates
        .iter()
        .filter(|candidate| candidate.parent_symbol_id.is_some() && candidate.kind == "function")
        .map(|candidate| &candidate.id)
        .collect();
    if !module_scoped_functions.is_empty() {
        return unique_id(&module_scoped_functions);
    }

    // A top-level type (struct/enum/protocol/interface/...) is a valid
    // construction/initializer target. Checked last — only when no function,
    // class, or method matched — so it never overrides existing resolution for
    // any language; it just lets languages whose constructible types are kind
    // `type` (e.g. Swift structs/enums) resolve their initializer calls.
    let types: Vec<&String> = candidates
        .iter()
        .filter(|candidate| candidate.parent_symbol_id.is_none() && candidate.kind == "type")
        .map(|candidate| &candidate.id)
        .collect();
    unique_id(&types)
}

fn select_default_import_candidate_id(candidates: &[LocalCalleeCandidate]) -> Option<String> {
    let top_level: Vec<&String> = candidates
        .iter()
        .filter(|candidate| {
            candidate.parent_symbol_id.is_none()
                && matches!(candidate.kind.as_str(), "function" | "class" | "type")
        })
        .map(|candidate| &candidate.id)
        .collect();
    unique_id(&top_level)
}

fn unique_id(ids: &[&String]) -> Option<String> {
    match ids {
        [single] => Some((*single).clone()),
        _ => None,
    }
}

fn non_empty(value: String) -> Option<String> {
    if value.is_empty() { None } else { Some(value) }
}

fn heritage_kind_from_str(value: &str) -> anyhow::Result<HeritageKind> {
    match value {
        "INHERITS" => Ok(HeritageKind::Inherits),
        "EXTENDS" => Ok(HeritageKind::Extends),
        "IMPLEMENTS" => Ok(HeritageKind::Implements),
        other => bail!("unknown code_inheritance.heritage_kind `{other}`"),
    }
}

fn call_target_kind_from_str(value: &str) -> anyhow::Result<CallTargetKind> {
    match value {
        "symbol" => Ok(CallTargetKind::Symbol),
        "unresolved" => Ok(CallTargetKind::Unresolved),
        "external" => Ok(CallTargetKind::External),
        // A completed index run rewrites every `local_import` row to `symbol` or
        // `unresolved`, but an interrupted run can leave one behind; parse it so
        // read-back (and the post-write resolver) never hard-errors.
        "local_import" => Ok(CallTargetKind::LocalImport),
        other => bail!("unknown code_calls.callee_target_kind `{other}`"),
    }
}

pub fn symbol_select_columns(alias: &str) -> String {
    assert!(
        safe_symbol_select_alias(alias),
        "symbol_select_columns alias must be empty or a safe SQL identifier"
    );
    let prefix = if alias.is_empty() {
        String::new()
    } else {
        format!("{alias}.")
    };
    format!(
        "{p}id, {p}project_id, {p}file_path, {p}name, {p}qualified_name, \
         {p}kind, {p}language, {p}byte_start::BIGINT AS byte_start, \
         {p}byte_end::BIGINT AS byte_end, {p}line_start::BIGINT AS line_start, \
         {p}line_end::BIGINT AS line_end, {p}signature, {p}docstring, \
         {p}parent_symbol_id, {p}file_content_hash, {p}content_hash, {p}summary, \
         {p}created_at::TEXT AS created_at, {p}updated_at::TEXT AS updated_at",
        p = prefix
    )
}

fn safe_symbol_select_alias(alias: &str) -> bool {
    if alias.is_empty() {
        return true;
    }
    let mut chars = alias.chars();
    chars
        .next()
        .is_some_and(|ch| ch == '_' || ch.is_ascii_alphabetic())
        && chars.all(|ch| ch == '_' || ch.is_ascii_alphanumeric())
}

#[cfg(test)]
#[path = "queries_cas_tests.rs"]
mod queries_cas_tests;

#[cfg(test)]
mod tests {
    use super::*;

    fn code_symbol_row(
        id: &str,
        kind: &str,
        parent_symbol_id: Option<&str>,
    ) -> LocalCalleeCandidate {
        LocalCalleeCandidate {
            id: id.to_string(),
            kind: kind.to_string(),
            parent_symbol_id: parent_symbol_id.map(str::to_string),
        }
    }

    #[test]
    fn resolves_unique_module_scoped_function_candidate() {
        let candidates = [code_symbol_row("greet-fn", "function", Some("app-greeter"))];

        assert_eq!(
            select_local_callee_candidate_id(&candidates),
            Some("greet-fn".to_string())
        );
    }

    #[test]
    fn method_tier_precedes_module_scoped_function_candidates() {
        let candidates = [
            code_symbol_row("greet-fn", "function", Some("app-greeter")),
            code_symbol_row("greet-method", "method", Some("app-greeter")),
        ];

        assert_eq!(
            select_local_callee_candidate_id(&candidates),
            Some("greet-method".to_string())
        );
    }

    #[test]
    fn leaves_ambiguous_module_scoped_function_candidates_unresolved() {
        let candidates = [
            code_symbol_row("greet-1", "function", Some("app-greeter")),
            code_symbol_row("greet-2", "function", Some("app-greeter")),
        ];

        assert_eq!(select_local_callee_candidate_id(&candidates), None);
    }

    #[test]
    fn default_import_selector_resolves_unique_top_level_candidate() {
        let candidates = [
            code_symbol_row("helper", "function", None),
            code_symbol_row("nested", "function", Some("helper")),
            code_symbol_row("method", "method", Some("helper")),
        ];

        assert_eq!(
            select_default_import_candidate_id(&candidates),
            Some("helper".to_string())
        );
    }

    #[test]
    fn default_import_selector_leaves_ambiguous_top_level_candidates_unresolved() {
        let candidates = [
            code_symbol_row("helper", "function", None),
            code_symbol_row("Widget", "class", None),
        ];

        assert_eq!(select_default_import_candidate_id(&candidates), None);
    }

    #[test]
    fn symbol_select_columns_accepts_empty_or_safe_alias() {
        assert!(symbol_select_columns("").starts_with("id, project_id"));
        assert!(symbol_select_columns("cs").starts_with("cs.id, cs.project_id"));
        assert!(symbol_select_columns("_symbols1").starts_with("_symbols1.id"));
    }

    #[test]
    #[should_panic(expected = "safe SQL identifier")]
    fn symbol_select_columns_rejects_unsafe_alias() {
        let _ = symbol_select_columns("cs;DROP TABLE code_symbols");
    }
}

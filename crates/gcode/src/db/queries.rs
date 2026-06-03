use anyhow::{Context as _, bail};
use postgres::GenericClient;

use crate::models::{CallRelation, CallTargetKind, ImportRelation, Symbol};

#[derive(Debug, Clone)]
pub struct GraphFileFacts {
    pub file_path: String,
    pub imports: Vec<ImportRelation>,
    pub definitions: Vec<Symbol>,
    pub calls: Vec<CallRelation>,
}

pub fn list_indexed_file_paths(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<Vec<String>> {
    let rows = conn.query(
        "SELECT file_path FROM code_indexed_files WHERE project_id = $1 ORDER BY file_path",
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
    let imports = read_imports_for_file(conn, project_id, file_path)?;
    let definitions = read_symbols_for_file(conn, project_id, file_path)?;
    let calls = read_calls_for_file(conn, project_id, file_path)?;

    Ok(GraphFileFacts {
        file_path: file_path.to_string(),
        imports,
        definitions,
        calls,
    })
}

pub fn indexed_file_exists(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<bool> {
    Ok(conn
        .query_opt(
            "SELECT 1 FROM code_indexed_files
             WHERE project_id = $1 AND file_path = $2",
            &[&project_id, &file_path],
        )?
        .is_some())
}

pub fn mark_graph_sync_attempted(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<bool> {
    let updated = conn.execute(
        "UPDATE code_indexed_files
         SET graph_synced = false, graph_sync_attempted_at = NOW()
         WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    )?;
    Ok(updated > 0)
}

pub fn mark_graph_synced(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<bool> {
    let updated = conn.execute(
        "UPDATE code_indexed_files
         SET graph_synced = true, graph_sync_attempted_at = NOW()
         WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    )?;
    Ok(updated > 0)
}

pub fn reset_graph_sync_for_project(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<u64> {
    Ok(conn.execute(
        "UPDATE code_indexed_files
         SET graph_synced = false, graph_sync_attempted_at = NULL
         WHERE project_id = $1",
        &[&project_id],
    )?)
}

pub fn mark_vectors_synced(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<bool> {
    let updated = conn.execute(
        "UPDATE code_indexed_files
         SET vectors_synced = true
         WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    )?;
    Ok(updated > 0)
}

pub fn mark_project_vectors_synced(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<u64> {
    Ok(conn.execute(
        "UPDATE code_indexed_files
         SET vectors_synced = true
         WHERE project_id = $1",
        &[&project_id],
    )?)
}

/// Return the vector sync state for an indexed file.
///
/// `None` means the file is not present in `code_indexed_files`; `Some(value)`
/// means the file exists and reports that `vectors_synced` state.
pub fn file_vectors_synced(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<Option<bool>> {
    let synced = conn
        .query_opt(
            "SELECT vectors_synced
             FROM code_indexed_files
             WHERE project_id = $1 AND file_path = $2",
            &[&project_id, &file_path],
        )?
        .map(|row| row.try_get::<_, bool>("vectors_synced"))
        .transpose()?;
    Ok(synced)
}

pub fn reset_vectors_sync_for_project(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<u64> {
    Ok(conn.execute(
        "UPDATE code_indexed_files
         SET vectors_synced = false
         WHERE project_id = $1",
        &[&project_id],
    )?)
}

fn read_imports_for_file(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<Vec<ImportRelation>> {
    let rows = conn.query(
        "SELECT source_file, target_module
         FROM code_imports
         WHERE project_id = $1 AND source_file = $2
         ORDER BY target_module",
        &[&project_id, &file_path],
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
) -> anyhow::Result<Vec<Symbol>> {
    let query = format!(
        "SELECT {} FROM code_symbols s
         WHERE s.project_id = $1 AND s.file_path = $2
         ORDER BY s.line_start, s.byte_start",
        symbol_select_columns("s")
    );
    let rows = conn.query(&query, &[&project_id, &file_path])?;
    rows.iter().map(Symbol::from_row).collect()
}

fn read_calls_for_file(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<Vec<CallRelation>> {
    let rows = conn.query(
        "SELECT caller_symbol_id, callee_symbol_id, callee_name,
                callee_target_kind, callee_external_module, file_path, line::BIGINT AS line
         FROM code_calls
         WHERE project_id = $1 AND file_path = $2
         ORDER BY line, caller_symbol_id, callee_name",
        &[&project_id, &file_path],
    )?;
    rows.into_iter()
        .map(|row| {
            let target_kind: String = row.try_get("callee_target_kind")?;
            let callee_symbol_id: String = row.try_get("callee_symbol_id")?;
            let callee_external_module: String = row.try_get("callee_external_module")?;
            Ok(CallRelation {
                caller_symbol_id: row.try_get("caller_symbol_id")?,
                callee_symbol_id: non_empty(callee_symbol_id),
                callee_name: row.try_get("callee_name")?,
                callee_target_kind: call_target_kind_from_str(&target_kind)?,
                callee_external_module: non_empty(callee_external_module),
                file_path: row.try_get("file_path")?,
                line: i64_to_usize(row.try_get("line")?, "line")?,
            })
        })
        .collect()
}

fn non_empty(value: String) -> Option<String> {
    if value.is_empty() { None } else { Some(value) }
}

fn call_target_kind_from_str(value: &str) -> anyhow::Result<CallTargetKind> {
    match value {
        "symbol" => Ok(CallTargetKind::Symbol),
        "unresolved" => Ok(CallTargetKind::Unresolved),
        "external" => Ok(CallTargetKind::External),
        other => bail!("unknown code_calls.callee_target_kind `{other}`"),
    }
}

fn i64_to_usize(value: i64, column: &str) -> anyhow::Result<usize> {
    value
        .try_into()
        .with_context(|| format!("column `{column}` contains negative or too-large value {value}"))
}

pub fn symbol_select_columns(alias: &str) -> String {
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
         {p}parent_symbol_id, {p}content_hash, {p}summary, \
         {p}created_at::TEXT AS created_at, {p}updated_at::TEXT AS updated_at",
        p = prefix
    )
}

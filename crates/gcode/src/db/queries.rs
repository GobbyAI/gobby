use anyhow::bail;
use postgres::GenericClient;

use crate::models::{CallRelation, CallTargetKind, ImportRelation, Symbol};
use crate::utils::i64_to_usize;

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
    rows.iter().map(call_relation_from_row).collect()
}

fn call_relation_from_row(row: &postgres::Row) -> anyhow::Result<CallRelation> {
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
    let rows = conn.query(
        "SELECT caller_symbol_id, callee_symbol_id, callee_name,
                callee_target_kind, callee_external_module, file_path, line::BIGINT AS line
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
    let rows = conn.query(
        "SELECT caller_symbol_id, callee_symbol_id, callee_name,
                callee_target_kind, callee_external_module, file_path, line::BIGINT AS line
         FROM code_calls
         WHERE project_id = $1 AND callee_target_kind = 'local_import'
         ORDER BY file_path, line, caller_symbol_id, callee_name",
        &[&project_id],
    )?;
    rows.iter().map(call_relation_from_row).collect()
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
    let rows = conn.query(
        "SELECT id, kind, parent_symbol_id
         FROM code_symbols
         WHERE project_id = $1 AND file_path = ANY($2) AND name = $3
         ORDER BY file_path, byte_start",
        &[&project_id, &target_files, &name],
    )?;

    let candidates: Vec<LocalCalleeCandidate> = rows
        .iter()
        .map(|row| {
            let id: String = row.try_get("id")?;
            let kind: String = row.try_get("kind")?;
            let parent_symbol_id: Option<String> = row.try_get("parent_symbol_id")?;
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
            let id: String = row.try_get("id")?;
            let kind: String = row.try_get("kind")?;
            let parent_symbol_id: Option<String> = row.try_get("parent_symbol_id")?;
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
         {p}parent_symbol_id, {p}content_hash, {p}summary, \
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

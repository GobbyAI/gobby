use postgres::GenericClient;
use uuid::Uuid;

use super::ids::{id_param, id_string, opt_id_string};

const MODULE_ROOT_FILES: &[&str] = &[
    "mod.rs",
    "lib.rs",
    "main.rs",
    "__init__.py",
    "__init__.pyi",
    "index.js",
    "index.jsx",
    "index.ts",
    "index.tsx",
    "index.mjs",
    "index.cjs",
];

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
///
/// When the exact-file query misses, module-root candidates (`mod.rs`, `lib.rs`,
/// `main.rs`, `__init__.py[i]`, `index.{js,jsx,ts,tsx,mjs,cjs}`) fall back to a
/// unique top-level `function`/`class`/`type` under that directory.
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
    let exact = query_exact_file_candidates(conn, &machine_id, &project_id, target_files, name)?;
    if !exact.is_empty() {
        return Ok(select_local_callee_candidate_id(&exact));
    }
    let prefixes = module_root_like_prefixes(target_files);
    if prefixes.is_empty() {
        return Ok(None);
    }
    let subtree = query_module_prefix_candidates(conn, &machine_id, &project_id, &prefixes, name)?;
    Ok(select_local_callee_candidate_id(&subtree))
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
    Ok(select_default_import_candidate_id(&candidates_from_rows(
        &rows,
    )?))
}

const LOCAL_CALLEE_FROM: &str = "
         FROM code_symbols s
         JOIN code_indexed_file_states cifs
           ON cifs.project_id = s.project_id
          AND cifs.file_path = s.file_path
          AND cifs.content_hash = s.file_content_hash
          AND cifs.machine_id = $1
         WHERE s.project_id = $2 AND s.name = $4";

fn query_exact_file_candidates(
    conn: &mut impl GenericClient,
    machine_id: &Uuid,
    project_id: &Uuid,
    target_files: &[String],
    name: &str,
) -> anyhow::Result<Vec<LocalCalleeCandidate>> {
    let sql = format!(
        "SELECT s.id, s.kind, s.parent_symbol_id{LOCAL_CALLEE_FROM}
          AND s.file_path = ANY($3)
         ORDER BY s.file_path, s.byte_start"
    );
    let rows = conn.query(&sql, &[machine_id, project_id, &target_files, &name])?;
    candidates_from_rows(&rows)
}

fn query_module_prefix_candidates(
    conn: &mut impl GenericClient,
    machine_id: &Uuid,
    project_id: &Uuid,
    prefixes: &[String],
    name: &str,
) -> anyhow::Result<Vec<LocalCalleeCandidate>> {
    let kinds = ["function", "class", "type"];
    let sql = format!(
        "SELECT s.id, s.kind, s.parent_symbol_id{LOCAL_CALLEE_FROM}
          AND s.file_path LIKE ANY($3) ESCAPE '#'
          AND s.parent_symbol_id IS NULL
          AND s.kind = ANY($5)
         ORDER BY s.file_path, s.byte_start"
    );
    let rows = conn.query(
        &sql,
        &[machine_id, project_id, &prefixes, &name, &kinds.as_slice()],
    )?;
    candidates_from_rows(&rows)
}

fn candidates_from_rows(rows: &[postgres::Row]) -> anyhow::Result<Vec<LocalCalleeCandidate>> {
    rows.iter()
        .map(|row| {
            let id = id_string(row, "id")?;
            let kind: String = row.try_get("kind")?;
            let parent_symbol_id = opt_id_string(row, "parent_symbol_id")?;
            Ok(LocalCalleeCandidate {
                id,
                kind,
                parent_symbol_id,
            })
        })
        .collect()
}

fn module_root_like_prefixes(target_files: &[String]) -> Vec<String> {
    let mut prefixes = Vec::new();
    for path in target_files {
        let Some(dir) = module_root_dir(path) else {
            continue;
        };
        let prefix = like_prefix(dir);
        if !prefixes.contains(&prefix) {
            prefixes.push(prefix);
        }
    }
    prefixes
}

fn module_root_dir(path: &str) -> Option<&str> {
    let (dir, name) = path.rsplit_once('/')?;
    MODULE_ROOT_FILES.contains(&name).then_some(dir)
}

fn like_prefix(dir: &str) -> String {
    let escaped = dir.replace('#', "##").replace('%', "#%").replace('_', "#_");
    format!("{escaped}/%")
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
    fn module_root_dir_maps_known_roots_and_ignores_plain_files() {
        assert_eq!(module_root_dir("pkg/__init__.py"), Some("pkg"));
        assert_eq!(module_root_dir("store/mod.rs"), Some("store"));
        assert_eq!(
            module_root_dir("crates/app/src/lib.rs"),
            Some("crates/app/src")
        );
        assert_eq!(module_root_dir("pkg/api.py"), None);
        assert_eq!(module_root_dir("index/api.rs"), None);
    }

    #[test]
    fn like_prefix_escapes_like_wildcards() {
        assert_eq!(like_prefix("pkg"), "pkg/%");
        assert_eq!(like_prefix("foo_bar"), "foo#_bar/%");
        assert_eq!(like_prefix("a%b#c"), "a#%b##c/%");
    }
}

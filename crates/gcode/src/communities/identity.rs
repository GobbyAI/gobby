//! Module-name ↔ file identity for the project import graph.

use std::collections::{BTreeSet, HashMap, HashSet};

use postgres::Client;

use crate::config::Context;
use crate::index::import_resolution::{ImportResolutionContext, build_import_resolution_context};
use crate::visibility;

/// Module-name ↔ file identity for the import graph.
///
/// `providers[module]` lists the visible files a module name can resolve to
/// across the whole project; `aliases[file]` lists the module names whose only
/// provider is that file. Alias membership is global: a relative specifier that
/// resolves to different files from different importers is ambiguous and
/// belongs to no file's equivalence class, because stored `IMPORTS` targets are
/// `CodeModule` nodes keyed by name and admit every consumer of that name.
#[derive(Clone, Debug)]
pub(crate) struct ImportIdentity {
    pub visible_files: HashSet<String>,
    pub providers: HashMap<String, Vec<String>>,
    pub aliases: HashMap<String, Vec<String>>,
}

impl ImportIdentity {
    pub(crate) fn providers_for(&self, module: &str) -> Vec<String> {
        self.providers
            .get(module)
            .into_iter()
            .flatten()
            .filter(|file| self.visible_files.contains(*file))
            .cloned()
            .collect()
    }

    /// The single visible file providing `module`, or `None` when the name is
    /// unknown or resolves to several files.
    pub(crate) fn unique_provider(&self, module: &str) -> Option<String> {
        match self.providers_for(module).as_slice() {
            [file] => Some(file.clone()),
            _ => None,
        }
    }

    /// One-shot identity build: `O(|rows| + |visible| + index)` resolver work.
    ///
    /// Every distinct module name (import targets plus the path-derived names of
    /// each visible file) gets its importer-independent candidates once; every
    /// distinct `(importer, module)` row adds the importer-aware candidates.
    pub(crate) fn from_resolution(
        visible: &HashSet<String>,
        resolver: &ImportResolutionContext,
        imports: &[(String, String)],
    ) -> Self {
        let rows = imports
            .iter()
            .map(|(source, module)| (source.as_str(), module.as_str()))
            .collect::<HashSet<_>>();
        let derived =
            resolver.path_derived_module_names_for_files(visible.iter().map(String::as_str));
        let modules = rows
            .iter()
            .map(|(_, module)| *module)
            .chain(derived.values().flatten().map(String::as_str))
            .collect::<HashSet<_>>();

        let mut providers: HashMap<String, BTreeSet<String>> = HashMap::new();
        for module in modules {
            providers.entry(module.to_string()).or_default().extend(
                resolver
                    .importer_independent_candidates(module)
                    .into_iter()
                    .filter(|file| visible.contains(file)),
            );
        }
        for (source, module) in &rows {
            providers.entry((*module).to_string()).or_default().extend(
                resolver
                    .importer_candidates(module, source)
                    .into_iter()
                    .filter(|file| visible.contains(file)),
            );
        }

        let mut aliases = visible
            .iter()
            .map(|file| (file.clone(), Vec::new()))
            .collect::<HashMap<String, Vec<String>>>();
        for (module, files) in &providers {
            if let [file] = files.iter().collect::<Vec<_>>().as_slice() {
                aliases
                    .entry((*file).clone())
                    .or_default()
                    .push(module.clone());
            }
        }
        for names in aliases.values_mut() {
            names.sort();
        }
        Self {
            visible_files: visible.clone(),
            providers: providers
                .into_iter()
                .map(|(module, files)| (module, files.into_iter().collect()))
                .collect(),
            aliases,
        }
    }
}

/// An identity and the `(file_path, module_name)` rows it was built from.
///
/// The rows are ordered by source project and then by `(file_path, module_name)`
/// within each project, which is how [`crate::db::read_active_imports`] returns them.
pub(crate) struct ProjectImports {
    pub identity: ImportIdentity,
    // `#[expect]` is wrong here: `cargo clippy --all-targets` compiles the test
    // cfg too, where the threshold spike reads these rows, so the expectation is
    // unfulfilled there. Plan 3.2 wires the lib read and drops the attribute.
    #[allow(dead_code)]
    pub rows: Vec<(String, String)>,
}

/// Build the project's import identity on a caller-supplied connection.
///
/// The caller owns the connection so an index run can call this inside its own
/// checkout fence rather than opening a second one against its own checkout.
pub(crate) fn load_project_imports(
    conn: &mut Client,
    ctx: &Context,
) -> anyhow::Result<ProjectImports> {
    let visible = visibility::visible_tree(conn, ctx)?
        .into_iter()
        .map(|file| file.file_path)
        .collect::<HashSet<_>>();
    let rows = visible_import_rows(conn, ctx)?;
    let candidates = visible
        .iter()
        .map(|path| ctx.project_root.join(path))
        .collect::<Vec<_>>();
    let resolver = build_import_resolution_context(&ctx.project_root, &candidates);
    let identity = ImportIdentity::from_resolution(&visible, &resolver, &rows);
    Ok(ProjectImports { identity, rows })
}

/// Import rows covering every file `visibility::visible_tree` reports.
///
/// `read_active_imports` is single-project, so an overlay context reads both
/// source projects and merges them under the same shadow rule `visible_tree`
/// applies; reading only the overlay's id would leave every parent-only file
/// edgeless.
fn visible_import_rows(conn: &mut Client, ctx: &Context) -> anyhow::Result<Vec<(String, String)>> {
    let project_ids = visibility::visible_project_ids(ctx);
    let Some((overlay_id, parent_ids)) = project_ids.split_first() else {
        return Ok(Vec::new());
    };
    let mut rows = import_rows(conn, overlay_id)?;
    if parent_ids.is_empty() {
        return Ok(rows);
    }
    let overlay_paths = crate::db::list_indexed_file_paths(conn, overlay_id)?
        .into_iter()
        .collect::<HashSet<_>>();
    for parent_id in parent_ids {
        rows = merge_visible_imports(rows, import_rows(conn, parent_id)?, &overlay_paths);
    }
    Ok(rows)
}

fn import_rows(conn: &mut Client, project_id: &str) -> anyhow::Result<Vec<(String, String)>> {
    Ok(crate::db::read_active_imports(conn, project_id)?
        .into_iter()
        .map(|row| (row.file_path, row.module_name))
        .collect())
}

/// Drop the parent rows the overlay shadows, keeping every other parent row.
///
/// `overlay_paths` is the overlay project's indexed file paths, tombstones
/// included, which is exactly what `visible_tree`'s anti-join tests: an overlay
/// row for a path replaces the parent's file, so the parent's imports for that
/// path describe content nobody can see.
fn merge_visible_imports(
    overlay_rows: Vec<(String, String)>,
    parent_rows: Vec<(String, String)>,
    overlay_paths: &HashSet<String>,
) -> Vec<(String, String)> {
    let mut merged = overlay_rows;
    merged.extend(
        parent_rows
            .into_iter()
            .filter(|(file_path, _)| !overlay_paths.contains(file_path)),
    );
    merged
}

#[cfg(test)]
#[path = "identity_tests.rs"]
pub(crate) mod identity_tests;

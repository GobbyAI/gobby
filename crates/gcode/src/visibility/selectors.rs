//! Effective file versions for text and symbol reads. A pin names facts, never
//! the source checkout's current selector.

use crate::config::{Context, ProjectIndexScope};

use super::TOMBSTONE_LANGUAGE;

pub(crate) fn scope_project_id(ctx: &Context) -> &str {
    match &ctx.index_scope {
        ProjectIndexScope::Single => &ctx.project_id,
        ProjectIndexScope::Overlay {
            overlay_project_id, ..
        } => overlay_project_id,
    }
}

/// Arguments are SQL placeholders chosen by internal query builders.
pub(crate) fn file_selectors_sql(machine: &str, project: &str, include_pins: bool) -> String {
    let owned = format!(
        "SELECT fs.project_id, fs.file_path, fs.content_hash, fs.git_blob_id,
                f.language, f.symbol_count
         FROM code_indexed_file_states fs
         JOIN code_indexed_files f
           ON f.project_id = fs.project_id AND f.file_path = fs.file_path
          AND f.content_hash = fs.content_hash
         WHERE fs.machine_id = {machine} AND fs.project_id = {project}
           AND f.language != '{TOMBSTONE_LANGUAGE}'"
    );
    if !include_pins {
        return owned;
    }
    format!(
        "{owned}
         UNION ALL
         SELECT p.source_project_id AS project_id, p.file_path, p.content_hash,
                p.git_blob_id, f.language, f.symbol_count
         FROM code_overlay_pins p
         JOIN code_indexed_files f
           ON f.project_id = p.source_project_id AND f.file_path = p.file_path
          AND f.content_hash = p.content_hash
         WHERE p.machine_id = {machine} AND p.overlay_project_id = {project}
           AND f.language != '{TOMBSTONE_LANGUAGE}'
           AND NOT EXISTS (
               SELECT 1 FROM code_indexed_file_states shadow
               WHERE shadow.machine_id = {machine} AND shadow.project_id = {project}
                 AND shadow.file_path = p.file_path
           )"
    )
}

pub(crate) fn visible_selectors_sql(ctx: &Context, machine: &str, project: &str) -> String {
    read_selectors_sql(
        machine,
        project,
        matches!(ctx.index_scope, ProjectIndexScope::Overlay { .. }),
        inherits_parent(ctx),
    )
}

/// Whether reads through this context may serve the parent's rows: a managed
/// principal reading an overlay. The SQL predicate narrows it to an overlay the
/// principal cannot write.
pub(crate) fn inherits_parent(ctx: &Context) -> bool {
    matches!(ctx.index_scope, ProjectIndexScope::Overlay { .. }) && ctx.is_managed_principal()
}

/// Read-side selectors. `inherit_parent` adds the parent's row for every path the
/// overlay neither shadows nor pins. Pins and overlay rows shadow it, so no path
/// is served twice; the stale check then names only paths that differ on disk.
pub(crate) fn read_selectors_sql(
    machine: &str,
    project: &str,
    include_pins: bool,
    inherit_parent: bool,
) -> String {
    let selectors = file_selectors_sql(machine, project, include_pins);
    if !inherit_parent {
        return selectors;
    }
    format!(
        "{selectors}
         UNION ALL
         SELECT fs.project_id, fs.file_path, fs.content_hash, fs.git_blob_id,
                f.language, f.symbol_count
         FROM code_indexed_file_states fs
         JOIN code_indexed_files f
           ON f.project_id = fs.project_id AND f.file_path = fs.file_path
          AND f.content_hash = fs.content_hash
         WHERE fs.machine_id = {machine}
           AND fs.project_id = gobby_agent_auth.current_project_id()
           AND f.language != '{TOMBSTONE_LANGUAGE}'
           AND COALESCE(gobby_agent_auth.current_code_overlay_project_id(),
                        gobby_agent_auth.current_project_id()) IS DISTINCT FROM {project}
           AND NOT EXISTS (
               SELECT 1 FROM code_indexed_file_states shadow
               WHERE shadow.machine_id = {machine} AND shadow.project_id = {project}
                 AND shadow.file_path = fs.file_path
           )
           AND NOT EXISTS (
               SELECT 1 FROM code_overlay_pins p
               WHERE p.machine_id = {machine} AND p.overlay_project_id = {project}
                 AND p.file_path = fs.file_path
           )"
    )
}

pub(crate) fn selector_condition(selectors: &str, alias: &str, hash_column: &str) -> String {
    format!(
        "EXISTS (SELECT 1 FROM ({selectors}) visible
                 WHERE visible.project_id = {alias}.project_id
                   AND visible.file_path = {alias}.file_path
                   AND visible.content_hash = {alias}.{hash_column})"
    )
}

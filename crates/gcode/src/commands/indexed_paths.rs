use std::collections::BTreeSet;

use postgres::Client;

use crate::commands::scope;
use crate::config::Context;
use crate::visibility;

#[cfg(test)]
thread_local! {
    static LOOKUPS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

pub(super) fn valid_indexed_paths(
    conn: &mut Client,
    ctx: &Context,
    file_paths: &BTreeSet<String>,
) -> BTreeSet<String> {
    let candidates: Vec<String> = file_paths.iter().cloned().collect();
    indexed_file_paths(conn, ctx, &candidates)
        .into_iter()
        .filter(|file_path| scope::path_exists_in_current_project(ctx, file_path))
        .collect()
}

/// The subset of `file_paths` that `visibility::indexed_file_exists` accepts for
/// each path, answered by one statement for the whole set.
fn indexed_file_paths(conn: &mut Client, ctx: &Context, file_paths: &[String]) -> BTreeSet<String> {
    if file_paths.is_empty() {
        return BTreeSet::new();
    }
    let Some(machine_id) = visibility::machine_uuid_or_invisible(&ctx.machine_id) else {
        return BTreeSet::new();
    };
    record_lookup();
    let Some(project_id) = visibility::project_uuid_or_invisible(visibility::scope_project_id(ctx))
    else {
        return BTreeSet::new();
    };
    let selectors = visibility::visible_selectors_sql(ctx, "$1", "$2");
    let rows = conn.query(
        &format!("SELECT file_path FROM ({selectors}) visible WHERE file_path = ANY($3)"),
        &[&machine_id, &project_id, &file_paths],
    );
    rows.unwrap_or_default()
        .iter()
        .filter_map(|row| row.try_get::<_, String>(0).ok())
        .collect()
}

#[cfg(test)]
fn record_lookup() {
    LOOKUPS.with(|count| count.set(count.get() + 1));
}

#[cfg(not(test))]
fn record_lookup() {}

#[cfg(test)]
pub(super) fn reset_lookup_count() {
    LOOKUPS.with(|count| count.set(0));
}

#[cfg(test)]
pub(super) fn lookup_count() -> usize {
    LOOKUPS.with(std::cell::Cell::get)
}

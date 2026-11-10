use postgres::GenericClient;

use crate::db;
use crate::models::Symbol;

use super::lifecycle::CodeSymbolVectorLifecycle;

/// Deletes the vector points for `file_path` that no longer name a symbol.
///
/// Deletion authority: a point goes only when its id is absent from `code_symbols` for
/// the project. The keep set is every symbol id the hub holds for the file across its
/// content versions, plus the ids this sync just upserted, so a pinned version's points
/// survive the sync that wrote them.
pub fn prune_stale_file_points(
    lifecycle: &CodeSymbolVectorLifecycle,
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
    symbols: &[Symbol],
) -> anyhow::Result<usize> {
    let mut keep = db::file_symbol_ids(conn, project_id, file_path)?;
    keep.extend(symbols.iter().map(|symbol| symbol.id.clone()));
    keep.sort();
    keep.dedup();
    Ok(lifecycle.prune_file_points(file_path, &keep)?)
}

use postgres::Client;

use super::ids::id_param;

/// Tracked paths that an overlay pin leaves unpinned, sorted by path.
///
/// The overlay indexer reads these to index paths no pin covers. Writes happen
/// with the pin rows in `commands::pin`.
pub fn persisted_gaps(
    conn: &mut Client,
    machine_id: &str,
    overlay_project_id: &str,
) -> anyhow::Result<Vec<String>> {
    let machine_id = id_param(machine_id)?;
    let overlay_project_id = id_param(overlay_project_id)?;
    let rows = conn.query(
        "SELECT file_path FROM code_overlay_pin_gaps
         WHERE machine_id = $1 AND overlay_project_id = $2
         ORDER BY file_path",
        &[&machine_id, &overlay_project_id],
    )?;
    Ok(rows.iter().map(|row| row.get(0)).collect())
}

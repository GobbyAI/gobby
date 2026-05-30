use postgres::GenericClient;

use crate::db;
use crate::models::Symbol;

pub fn fetch_symbols_for_file(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<Vec<Symbol>> {
    let columns = db::symbol_select_columns("");
    conn.query(
        &format!(
            "SELECT {columns} FROM code_symbols
             WHERE project_id = $1 AND file_path = $2
             ORDER BY file_path, byte_start, id"
        ),
        &[&project_id, &file_path],
    )?
    .into_iter()
    .map(|row| Symbol::from_row(&row))
    .collect()
}

pub fn fetch_symbols_for_project(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<Vec<Symbol>> {
    let columns = db::symbol_select_columns("");
    conn.query(
        &format!(
            "SELECT {columns} FROM code_symbols
             WHERE project_id = $1
             ORDER BY file_path, byte_start, id"
        ),
        &[&project_id],
    )?
    .into_iter()
    .map(|row| Symbol::from_row(&row))
    .collect()
}

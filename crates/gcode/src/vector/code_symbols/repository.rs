use postgres::GenericClient;

use crate::db;
use crate::models::Symbol;

pub fn fetch_symbols_for_file(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<Vec<Symbol>> {
    fetch_symbols_where(
        conn,
        "project_id = $1 AND file_path = $2",
        &[&project_id, &file_path],
    )
}

pub fn fetch_symbols_for_project(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<Vec<Symbol>> {
    fetch_symbols_where(conn, "project_id = $1", &[&project_id])
}

fn fetch_symbols_where(
    conn: &mut impl GenericClient,
    predicate: &str,
    params: &[&(dyn postgres::types::ToSql + Sync)],
) -> anyhow::Result<Vec<Symbol>> {
    let columns = db::symbol_select_columns("");
    conn.query(
        &format!(
            "SELECT {columns} FROM code_symbols
             WHERE {predicate}
             ORDER BY file_path, byte_start, id"
        ),
        params,
    )?
    .into_iter()
    .map(|row| Symbol::from_row(&row))
    .collect()
}

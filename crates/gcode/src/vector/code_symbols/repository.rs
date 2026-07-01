use postgres::{GenericClient, types::ToSql};

use crate::db;
use crate::models::Symbol;

pub fn fetch_symbols_for_file(
    conn: &mut impl GenericClient,
    project_id: &str,
    file_path: &str,
) -> anyhow::Result<Vec<Symbol>> {
    fetch_symbols_where(
        conn,
        SymbolPredicate::File {
            project_id,
            file_path,
        },
    )
}

pub fn fetch_symbols_for_project(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<Vec<Symbol>> {
    fetch_symbols_where(conn, SymbolPredicate::Project { project_id })
}

enum SymbolPredicate<'a> {
    Project {
        project_id: &'a str,
    },
    File {
        project_id: &'a str,
        file_path: &'a str,
    },
}

impl SymbolPredicate<'_> {
    fn where_clause(&self) -> &'static str {
        match self {
            Self::Project { .. } => "project_id = $1",
            Self::File { .. } => "project_id = $1 AND file_path = $2",
        }
    }

    fn params(&self) -> Vec<&(dyn ToSql + Sync)> {
        match self {
            Self::Project { project_id } => vec![project_id as &(dyn ToSql + Sync)],
            Self::File {
                project_id,
                file_path,
            } => vec![
                project_id as &(dyn ToSql + Sync),
                file_path as &(dyn ToSql + Sync),
            ],
        }
    }
}

fn fetch_symbols_where(
    conn: &mut impl GenericClient,
    predicate: SymbolPredicate<'_>,
) -> anyhow::Result<Vec<Symbol>> {
    let columns = db::symbol_select_columns("");
    let params = predicate.params();
    conn.query(
        &format!(
            "SELECT {columns} FROM code_symbols
             WHERE {}
             ORDER BY file_path, byte_start, id",
            predicate.where_clause()
        ),
        &params,
    )?
    .into_iter()
    .map(|row| Symbol::from_row(&row))
    .collect()
}

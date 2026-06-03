use std::collections::HashSet;

use postgres::{Client, Row};

use crate::db;
use crate::models::Symbol;

use super::common::ResolvedGraphSymbol;
use super::symbols::{search_symbols_by_name, search_symbols_fts};

const EXACT_ID_MATCH_LIMIT: usize = 2;
const EXACT_QUALIFIED_NAME_MATCH_LIMIT: usize = 6;
const EXACT_NAME_MATCH_LIMIT: usize = 6;
const FUZZY_NAME_MATCH_LIMIT: usize = 6;

fn exact_symbol_matches_result(
    conn: &mut Client,
    project_id: &str,
    column: &str,
    input: &str,
    limit: usize,
) -> anyhow::Result<Vec<Symbol>> {
    let columns = db::symbol_select_columns("");
    let column = match column {
        "id" | "qualified_name" | "name" => column,
        _ => return Ok(Vec::new()),
    };
    let sql = format!(
        "SELECT {columns}
         FROM code_symbols
         WHERE project_id = $1 AND {column} = $2
         ORDER BY file_path ASC, line_start ASC
         LIMIT $3"
    );
    let rows = conn.query(&sql, &[&project_id, &input, &(limit as i64)])?;
    let mut symbols = Vec::new();
    for row in &rows {
        match Symbol::from_row(row) {
            Ok(symbol) => symbols.push(symbol),
            Err(error) => log::warn!(
                "discarding malformed graph symbol row during exact {column} lookup \
                 for project_id={project_id} input={input:?}: id={} name={} file_path={}: {error}",
                row_string(row, "id"),
                row_string(row, "name"),
                row_string(row, "file_path"),
            ),
        }
    }
    Ok(symbols)
}

fn row_string(row: &Row, column: &str) -> String {
    row.try_get::<_, String>(column)
        .unwrap_or_else(|_| "<unavailable>".to_string())
}

fn suggestion_label(symbol: &Symbol) -> String {
    format!(
        "{} ({}:{})",
        symbol.qualified_name, symbol.file_path, symbol.line_start
    )
}

fn resolved_symbol(symbol: &Symbol) -> ResolvedGraphSymbol {
    ResolvedGraphSymbol {
        id: symbol.id.clone(),
        display_name: symbol.name.clone(),
    }
}

pub fn resolve_graph_symbol_by_id(
    conn: &mut Client,
    symbol_id: &str,
    project_id: &str,
) -> anyhow::Result<Option<ResolvedGraphSymbol>> {
    let matches = exact_symbol_matches_result(conn, project_id, "id", symbol_id, 1)?;
    Ok(matches.first().map(resolved_symbol))
}

fn resolve_from_candidates(candidates: Vec<Symbol>) -> (Option<ResolvedGraphSymbol>, Vec<String>) {
    match candidates.len() {
        0 => (None, vec![]),
        1 => (Some(resolved_symbol(&candidates[0])), vec![]),
        _ => {
            let mut suggestions = Vec::new();
            let mut seen = HashSet::new();
            for symbol in &candidates {
                let label = suggestion_label(symbol);
                if seen.insert(label.clone()) {
                    suggestions.push(label);
                }
            }
            (None, suggestions)
        }
    }
}

fn decisive_resolution(
    candidates: Vec<Symbol>,
) -> Option<(Option<ResolvedGraphSymbol>, Vec<String>)> {
    let (resolved, suggestions) = resolve_from_candidates(candidates);
    (resolved.is_some() || !suggestions.is_empty()).then_some((resolved, suggestions))
}

/// Resolve user input to a canonical symbol id for graph queries.
///
/// Resolution is fail-closed: ambiguous matches return `None` with suggestions.
pub fn resolve_graph_symbol(
    conn: &mut Client,
    input: &str,
    project_id: &str,
) -> anyhow::Result<(Option<ResolvedGraphSymbol>, Vec<String>)> {
    for (column, limit) in [
        ("id", EXACT_ID_MATCH_LIMIT),
        ("qualified_name", EXACT_QUALIFIED_NAME_MATCH_LIMIT),
        ("name", EXACT_NAME_MATCH_LIMIT),
    ] {
        if let Some(result) = decisive_resolution(exact_symbol_matches_result(
            conn, project_id, column, input, limit,
        )?) {
            return Ok(result);
        }
    }

    if let Some(result) = decisive_resolution(search_symbols_by_name(
        conn,
        input,
        project_id,
        None,
        None,
        &[],
        FUZZY_NAME_MATCH_LIMIT,
    )) {
        return Ok(result);
    }

    let fts_results = search_symbols_fts(
        conn,
        input,
        project_id,
        None,
        None,
        &[],
        FUZZY_NAME_MATCH_LIMIT,
    );
    Ok(resolve_from_candidates(fts_results))
}

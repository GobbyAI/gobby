use std::collections::HashSet;

use postgres::Client;

use crate::db;
use crate::models::Symbol;

use super::common::ResolvedGraphSymbol;
use super::symbols::{search_symbols_by_name, search_symbols_fts};

fn exact_symbol_matches(
    conn: &mut Client,
    project_id: &str,
    column: &str,
    input: &str,
    limit: usize,
) -> Vec<Symbol> {
    if !matches!(column, "id" | "qualified_name" | "name") {
        return Vec::new();
    }
    let columns = db::symbol_select_columns("");
    let sql = format!(
        "SELECT {columns}
         FROM code_symbols
         WHERE project_id = $1 AND {column} = $2
         ORDER BY file_path ASC, line_start ASC
         LIMIT $3"
    );
    conn.query(&sql, &[&project_id, &input, &(limit as i64)])
        .ok()
        .map(|rows| {
            rows.iter()
                .filter_map(|row| Symbol::from_row(row).ok())
                .collect()
        })
        .unwrap_or_default()
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

/// Resolve user input to a canonical symbol id for graph queries.
///
/// Resolution is fail-closed: ambiguous matches return `None` with suggestions.
pub fn resolve_graph_symbol(
    conn: &mut Client,
    input: &str,
    project_id: &str,
) -> (Option<ResolvedGraphSymbol>, Vec<String>) {
    let ids = exact_symbol_matches(conn, project_id, "id", input, 2);
    let (resolved, suggestions) = resolve_from_candidates(ids);
    if resolved.is_some() || !suggestions.is_empty() {
        return (resolved, suggestions);
    }

    let qualified = exact_symbol_matches(conn, project_id, "qualified_name", input, 6);
    let (resolved, suggestions) = resolve_from_candidates(qualified);
    if resolved.is_some() || !suggestions.is_empty() {
        return (resolved, suggestions);
    }

    let exact = exact_symbol_matches(conn, project_id, "name", input, 6);
    let (resolved, suggestions) = resolve_from_candidates(exact);
    if resolved.is_some() || !suggestions.is_empty() {
        return (resolved, suggestions);
    }

    let like_matches = search_symbols_by_name(conn, input, project_id, None, None, &[], 6);
    let (resolved, suggestions) = resolve_from_candidates(like_matches);
    if resolved.is_some() || !suggestions.is_empty() {
        return (resolved, suggestions);
    }

    let fts_results = search_symbols_fts(conn, input, project_id, None, None, &[], 6);
    resolve_from_candidates(fts_results)
}

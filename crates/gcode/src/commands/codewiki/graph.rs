use super::*;
use crate::config::Context;
use gobby_core::falkor::{GraphClient, Row};

pub(crate) fn fetch_codewiki_graph_edges(
    ctx: &Context,
    files: &[String],
    symbols: &[Symbol],
    edge_limit: usize,
) -> anyhow::Result<CodewikiGraph> {
    let core_symbol_ids = symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .map(|symbol| symbol.id.clone())
        .collect::<HashSet<_>>();
    if core_symbol_ids.is_empty() {
        return Ok(CodewikiGraph::available(Vec::new()));
    }

    let Some(config) = &ctx.falkordb else {
        return Ok(CodewikiGraph::unavailable());
    };

    let core_files = files
        .iter()
        .filter(|file| is_core_file(file))
        .cloned()
        .collect::<Vec<_>>();

    let connection_config = config.connection_config();
    let Some(raw) = fetch_graph_rows(
        &connection_config,
        &config.graph_name,
        &ctx.project_id,
        edge_limit,
        !core_files.is_empty(),
        ctx.quiet,
    ) else {
        return Ok(CodewikiGraph::unavailable());
    };

    let mut edges = Vec::new();
    // FalkorDB only reports that at most LIMIT rows were returned, so equality
    // is the conservative signal that additional rows may have been omitted.
    let mut truncated = raw.call_truncated;
    for (source, target) in raw.call_pairs {
        if !core_symbol_ids.contains(&source) {
            continue;
        }
        if !core_symbol_ids.contains(&target) {
            continue;
        }
        edges.push(CodewikiGraphEdge::call(source, target));
    }

    if !core_files.is_empty() {
        let file_symbols = symbols_by_file_component(symbols);
        // A full import page may be exactly complete or may have hidden rows;
        // mark it truncated so rendered docs disclose that uncertainty.
        truncated |= raw.import_truncated;
        edges.extend(import_edges_from_pairs(
            &raw.import_pairs,
            &core_files,
            &file_symbols,
        ));
    }

    if truncated {
        Ok(CodewikiGraph::truncated(edges))
    } else {
        Ok(CodewikiGraph::available(edges))
    }
}

/// Raw `(source, target)` pairs pulled from FalkorDB before core-membership
/// filtering, plus the per-query truncation signals.
struct RawGraphRows {
    call_pairs: Vec<(String, String)>,
    import_pairs: Vec<(String, String)>,
    call_truncated: bool,
    import_truncated: bool,
}

/// Connect to FalkorDB and pull the call/import edge rows. Any connection or
/// query error degrades to `None`; the caller treats that as an unavailable
/// graph. Socket connect/read/write timeouts are enforced by `GraphClient`.
fn fetch_graph_rows(
    connection_config: &gobby_core::config::FalkorConfig,
    graph_name: &str,
    project_id: &str,
    edge_limit: usize,
    need_import: bool,
    quiet: bool,
) -> Option<RawGraphRows> {
    let mut client = match GraphClient::from_config(connection_config, graph_name) {
        Ok(client) => client,
        Err(e) => {
            if !quiet {
                eprintln!("Warning: FalkorDB connection failed: {e}");
            }
            return None;
        }
    };

    let (query, params) = codewiki_call_edges_query(project_id, edge_limit);
    let call_rows = match client.query(&query, Some(params)) {
        Ok(rows) => rows,
        Err(e) => {
            if !quiet {
                eprintln!("Warning: FalkorDB query failed: {e}");
            }
            return None;
        }
    };
    let call_truncated = call_rows.len() == edge_limit;
    let call_pairs = rows_to_pairs(&call_rows);

    let (import_pairs, import_truncated) = if need_import {
        let (query, params) = codewiki_import_edges_query(project_id, edge_limit);
        let import_rows = match client.query(&query, Some(params)) {
            Ok(rows) => rows,
            Err(e) => {
                if !quiet {
                    eprintln!("Warning: FalkorDB query failed: {e}");
                }
                return None;
            }
        };
        let truncated = import_rows.len() == edge_limit;
        (rows_to_pairs(&import_rows), truncated)
    } else {
        (Vec::new(), false)
    };

    Some(RawGraphRows {
        call_pairs,
        import_pairs,
        call_truncated,
        import_truncated,
    })
}

/// Extract `(source, target)` string pairs from FalkorDB rows, skipping any row
/// missing either column.
fn rows_to_pairs(rows: &[Row]) -> Vec<(String, String)> {
    rows.iter()
        .filter_map(|row| {
            let source = row.get("source").and_then(|value| value.as_str())?;
            let target = row.get("target").and_then(|value| value.as_str())?;
            Some((source.to_string(), target.to_string()))
        })
        .collect()
}

/// Resolve project-scoped import rows into component edges, keeping only
/// edges whose source file is core (the query itself is unfiltered).
pub(crate) fn import_edges_from_pairs(
    pairs: &[(String, String)],
    core_files: &[String],
    file_symbols: &BTreeMap<String, Vec<String>>,
) -> Vec<CodewikiGraphEdge> {
    let core_file_set = core_files
        .iter()
        .map(String::as_str)
        .collect::<HashSet<_>>();
    let mut edges = Vec::new();
    for (source_file, target_module) in pairs {
        if !core_file_set.contains(source_file.as_str()) {
            continue;
        }
        let Some(source_component_id) = first_component_for_file(file_symbols, source_file) else {
            continue;
        };
        for target_file in files_for_import_target(core_files, target_module) {
            let Some(target_component_id) = first_component_for_file(file_symbols, target_file)
            else {
                continue;
            };
            edges.push(CodewikiGraphEdge::import(
                source_component_id.clone(),
                target_component_id,
            ));
        }
    }
    edges
}

// Both edge queries are project-scoped only. Embedding the core symbol-id or
// file lists in the Cypher text produced payloads in the hundreds of kilobytes
// (every core symbol UUID twice), which intermittently failed at the socket
// layer; core filtering happens client-side in fetch_codewiki_graph_edges.
pub(crate) fn codewiki_call_edges_query(
    project_id: &str,
    edge_limit: usize,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[:CALLS]->(target:CodeSymbol {{project: $project}}) \
             RETURN source.id AS source, target.id AS target \
             LIMIT {edge_limit}"
        ),
        HashMap::from([(
            "project".to_string(),
            typed_query::cypher_string_literal(project_id),
        )]),
    )
}

pub(crate) fn codewiki_import_edges_query(
    project_id: &str,
    edge_limit: usize,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (source:CodeFile {{project: $project}})-[:IMPORTS]->(target:CodeModule {{project: $project}}) \
             RETURN source.path AS source, target.name AS target \
             LIMIT {edge_limit}"
        ),
        HashMap::from([(
            "project".to_string(),
            typed_query::cypher_string_literal(project_id),
        )]),
    )
}

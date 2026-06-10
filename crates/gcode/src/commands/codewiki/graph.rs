use super::*;
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

    let connection_config = config.connection_config();
    let mut client = match GraphClient::from_config(&connection_config, &config.graph_name) {
        Ok(client) => client,
        Err(e) => {
            if !ctx.quiet {
                eprintln!("Warning: FalkorDB connection failed: {e}");
            }
            return Ok(CodewikiGraph::unavailable());
        }
    };

    fn query_or_unavailable(
        ctx: &Context,
        client: &mut GraphClient,
        query: &str,
        params: HashMap<String, String>,
    ) -> Option<Vec<Row>> {
        match client.query(query, Some(params)) {
            Ok(rows) => Some(rows),
            Err(e) => {
                if !ctx.quiet {
                    eprintln!("Warning: FalkorDB query failed: {e}");
                }
                None
            }
        }
    }

    let core_files = files
        .iter()
        .filter(|file| is_core_file(file))
        .cloned()
        .collect::<Vec<_>>();

    let mut edges = Vec::new();
    let (query, params) = codewiki_call_edges_query(&ctx.project_id, edge_limit);
    let Some(rows) = query_or_unavailable(ctx, &mut client, &query, params) else {
        return Ok(CodewikiGraph::unavailable());
    };
    // FalkorDB only reports that at most LIMIT rows were returned, so equality
    // is the conservative signal that additional rows may have been omitted.
    let mut truncated = rows.len() == edge_limit;
    for row in rows {
        let Some(source) = row.get("source").and_then(|value| value.as_str()) else {
            continue;
        };
        let Some(target) = row.get("target").and_then(|value| value.as_str()) else {
            continue;
        };
        if !core_symbol_ids.contains(source) {
            continue;
        };
        if !core_symbol_ids.contains(target) {
            continue;
        };
        edges.push(CodewikiGraphEdge::call(
            source.to_string(),
            target.to_string(),
        ));
    }

    if !core_files.is_empty() {
        let file_symbols = symbols_by_file_component(symbols);
        let (query, params) = codewiki_import_edges_query(&ctx.project_id, edge_limit);
        let Some(rows) = query_or_unavailable(ctx, &mut client, &query, params) else {
            return Ok(CodewikiGraph::unavailable());
        };
        // A full import page may be exactly complete or may have hidden rows;
        // mark it truncated so rendered docs disclose that uncertainty.
        truncated |= rows.len() == edge_limit;
        let pairs = rows
            .iter()
            .filter_map(|row| {
                let source = row.get("source").and_then(|value| value.as_str())?;
                let target = row.get("target").and_then(|value| value.as_str())?;
                Some((source.to_string(), target.to_string()))
            })
            .collect::<Vec<_>>();
        edges.extend(import_edges_from_pairs(&pairs, &core_files, &file_symbols));
    }

    if truncated {
        Ok(CodewikiGraph::truncated(edges))
    } else {
        Ok(CodewikiGraph::available(edges))
    }
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

use super::*;

pub(crate) fn fetch_codewiki_graph_edges(
    ctx: &Context,
    files: &[String],
    symbols: &[Symbol],
    edge_limit: usize,
) -> anyhow::Result<CodewikiGraph> {
    let symbol_components = symbols
        .iter()
        .filter(|symbol| is_core_file(&symbol.file_path))
        .map(|symbol| (symbol.id.clone(), component_id(symbol)))
        .collect::<HashMap<_, _>>();
    if symbol_components.is_empty() {
        return Ok(CodewikiGraph::available(Vec::new()));
    }

    let Some(config) = &ctx.falkordb else {
        return Ok(CodewikiGraph::unavailable());
    };

    let mut client = match falkor::FalkorClient::from_config(config) {
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
        client: &mut falkor::FalkorClient,
        query: &str,
        params: HashMap<String, String>,
    ) -> Option<Vec<falkor::Row>> {
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

    let symbol_ids = symbol_components.keys().cloned().collect::<Vec<_>>();
    let core_files = files
        .iter()
        .filter(|file| is_core_file(file))
        .cloned()
        .collect::<Vec<_>>();

    let mut edges = Vec::new();
    let (query, params) = codewiki_call_edges_query(&ctx.project_id, &symbol_ids, edge_limit);
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
        let Some(source_component_id) = symbol_components.get(source).cloned() else {
            continue;
        };
        let Some(target_component_id) = symbol_components.get(target).cloned() else {
            continue;
        };
        edges.push(CodewikiGraphEdge::call(
            source_component_id,
            target_component_id,
        ));
    }

    if !core_files.is_empty() {
        let file_symbols = symbols_by_file_component(symbols);
        let (query, params) = codewiki_import_edges_query(&ctx.project_id, &core_files, edge_limit);
        let Some(rows) = query_or_unavailable(ctx, &mut client, &query, params) else {
            return Ok(CodewikiGraph::unavailable());
        };
        // A full import page may be exactly complete or may have hidden rows;
        // mark it truncated so rendered docs disclose that uncertainty.
        truncated |= rows.len() == edge_limit;
        for row in rows {
            let Some(source_file) = row.get("source").and_then(|value| value.as_str()) else {
                continue;
            };
            let Some(target_module) = row.get("target").and_then(|value| value.as_str()) else {
                continue;
            };
            let Some(source_component_id) = first_component_for_file(&file_symbols, source_file)
            else {
                continue;
            };
            for target_file in files_for_import_target(&core_files, target_module) {
                let Some(target_component_id) =
                    first_component_for_file(&file_symbols, target_file)
                else {
                    continue;
                };
                edges.push(CodewikiGraphEdge::import(
                    source_component_id.clone(),
                    target_component_id,
                ));
            }
        }
    }

    if truncated {
        Ok(CodewikiGraph::truncated(edges))
    } else {
        Ok(CodewikiGraph::available(edges))
    }
}

pub(crate) fn codewiki_call_edges_query(
    project_id: &str,
    symbol_ids: &[String],
    edge_limit: usize,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (source:CodeSymbol {{project: $project}})-[:CALLS]->(target:CodeSymbol {{project: $project}}) \
             WHERE source.id IN [{}] AND target.id IN [{}] \
             RETURN source.id AS source, target.id AS target \
             LIMIT {edge_limit}",
            falkor::id_list_literal(symbol_ids),
            falkor::id_list_literal(symbol_ids)
        ),
        HashMap::from([(
            "project".to_string(),
            falkor::cypher_string_literal(project_id),
        )]),
    )
}

pub(crate) fn codewiki_import_edges_query(
    project_id: &str,
    files: &[String],
    edge_limit: usize,
) -> (String, HashMap<String, String>) {
    (
        format!(
            "MATCH (source:CodeFile {{project: $project}})-[:IMPORTS]->(target:CodeModule {{project: $project}}) \
             WHERE source.path IN [{}] \
             RETURN source.path AS source, target.name AS target \
             LIMIT {edge_limit}",
            falkor::id_list_literal(files)
        ),
        HashMap::from([(
            "project".to_string(),
            falkor::cypher_string_literal(project_id),
        )]),
    )
}

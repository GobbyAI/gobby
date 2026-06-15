use crate::config::Context;
use crate::graph::typed_query;

use super::super::connection::with_optional_core_graph;
use super::super::payload::{
    GraphBlastRadiusTarget, GraphLink, GraphNode, GraphPayload, add_link_from_row,
    add_node_from_row, add_prefixed_node_from_row, row_string_owned, row_to_projection_metadata,
    row_usize,
};
use super::payload_queries::{
    blast_radius_center_query, blast_radius_file_call_query, blast_radius_file_import_query,
    file_calls_query, file_symbols_query, project_overview_calls_query,
    project_overview_defines_query, project_overview_files_query, project_overview_imports_query,
    symbol_neighbors_query,
};
use super::relationship_queries::blast_radius_query;
use super::support::{clamp_limit, dedupe_limited_blast_rows};

pub fn project_overview_graph(ctx: &Context, limit: usize) -> anyhow::Result<GraphPayload> {
    with_optional_core_graph(ctx, GraphPayload::default, |client| {
        let limit = clamp_limit(limit);
        let link_limit = clamp_limit(limit.saturating_mul(4));
        let max_nodes = limit.saturating_mul(8);

        let (query, params) = project_overview_files_query(&ctx.project_id, limit);
        let file_rows = client.query(&query, Some(params))?;
        let mut payload = GraphPayload::default();
        for row in &file_rows {
            add_node_from_row(&mut payload, row, "file");
        }

        let file_paths = payload
            .nodes()
            .iter()
            .filter(|node| node.node_type == "file")
            .map(|node| node.id.clone())
            .collect::<Vec<_>>();
        if file_paths.is_empty() {
            return Ok(payload);
        }

        let (query, params) =
            project_overview_imports_query(&ctx.project_id, &file_paths, link_limit);
        for row in client.query(&query, Some(params))? {
            add_link_from_row(&mut payload, &row);
            if let Some(module_id) = row_string_owned(&row, &["target"]) {
                payload.push_node(GraphNode::new(module_id.clone(), module_id, "module"));
            }
            if payload.node_count() >= max_nodes {
                break;
            }
        }

        let (query, params) =
            project_overview_defines_query(&ctx.project_id, &file_paths, link_limit);
        for row in client.query(&query, Some(params))? {
            add_link_from_row(&mut payload, &row);
            if let Some(symbol_id) = row_string_owned(&row, &["target"]) {
                let mut node = GraphNode::new(
                    symbol_id.clone(),
                    row_string_owned(&row, &["symbol_name"]).unwrap_or(symbol_id),
                    row_string_owned(&row, &["symbol_kind"])
                        .unwrap_or_else(|| "function".to_string()),
                );
                node.kind = row_string_owned(&row, &["symbol_kind"]);
                node.file_path = row_string_owned(&row, &["symbol_file_path", "source"]);
                node.line_start = row_usize(&row, &["line_start"]);
                payload.push_node(node);
            }
            if payload.node_count() >= max_nodes {
                break;
            }
        }

        let (query, params) =
            project_overview_calls_query(&ctx.project_id, &file_paths, link_limit);
        for row in client.query(&query, Some(params))? {
            add_link_from_row(&mut payload, &row);
            if let Some(target_id) = row_string_owned(&row, &["target"]) {
                let mut node = GraphNode::new(
                    target_id.clone(),
                    row_string_owned(&row, &["target_name"]).unwrap_or(target_id),
                    row_string_owned(&row, &["target_type"])
                        .unwrap_or_else(|| "unresolved".to_string()),
                );
                node.kind = row_string_owned(&row, &["target_kind"]);
                node.file_path = row_string_owned(&row, &["target_file_path"]);
                node.line_start = row_usize(&row, &["target_line_start"]);
                payload.push_node(node);
            }
            if payload.node_count() >= max_nodes {
                break;
            }
        }

        Ok(payload)
    })
}

pub fn file_graph(ctx: &Context, file_path: &str) -> anyhow::Result<GraphPayload> {
    with_optional_core_graph(ctx, GraphPayload::default, |client| {
        let mut payload = GraphPayload::default();
        let mut file_node = GraphNode::new(file_path, file_path, "file");
        file_node.file_path = Some(file_path.to_string());
        payload.push_node(file_node);

        let (query, params) = file_symbols_query(&ctx.project_id, file_path);
        for row in client.query(&query, Some(params))? {
            add_node_from_row(&mut payload, &row, "function");
            if let Some(symbol_id) = row_string_owned(&row, &["id"]) {
                let mut link = GraphLink::new(file_path, symbol_id, "DEFINES");
                link.metadata = row_to_projection_metadata(&row);
                payload.links.push(link);
            }
        }

        let (query, params) = file_calls_query(&ctx.project_id, file_path);
        for row in client.query(&query, Some(params))? {
            add_prefixed_node_from_row(&mut payload, &row, "source", "function");
            add_prefixed_node_from_row(&mut payload, &row, "target", "unresolved");
            add_link_from_row(&mut payload, &row);
        }

        Ok(payload)
    })
}

pub fn symbol_neighbors(
    ctx: &Context,
    symbol_id: &str,
    limit: usize,
) -> anyhow::Result<GraphPayload> {
    with_optional_core_graph(ctx, GraphPayload::default, |client| {
        let mut payload = GraphPayload::with_center(symbol_id.to_string());
        let (query, params) = blast_radius_center_query(&ctx.project_id, symbol_id);
        let center_rows = client.query(&query, Some(params))?;
        let center_node = center_rows
            .first()
            .and_then(|row| GraphNode::from_row(row, "function"))
            .unwrap_or_else(|| GraphNode::new(symbol_id, symbol_id, "function"));
        payload.push_node(center_node);

        let (query, params) = symbol_neighbors_query(&ctx.project_id, symbol_id, limit);
        let rows = client.query(&query, Some(params))?;

        for row in rows {
            add_node_from_row(&mut payload, &row, "unresolved");
            let Some(neighbor_id) = row_string_owned(&row, &["id"]) else {
                continue;
            };
            let direction = row_string_owned(&row, &["direction"]).unwrap_or_default();
            let mut link = if direction == "outgoing" {
                GraphLink::new(symbol_id, neighbor_id, "CALLS")
            } else {
                GraphLink::new(neighbor_id, symbol_id, "CALLS")
            };
            link.line = row_usize(&row, &["line"]);
            link.metadata = row_to_projection_metadata(&row);
            payload.links.push(link);
        }

        Ok(payload)
    })
}

pub fn blast_radius_graph(
    ctx: &Context,
    target: GraphBlastRadiusTarget,
    depth: usize,
    limit: usize,
) -> anyhow::Result<GraphPayload> {
    with_optional_core_graph(ctx, GraphPayload::default, |client| {
        let (center_id, mut center_node, rows) = match target {
            GraphBlastRadiusTarget::SymbolId(symbol_id) => {
                let (query, params) = blast_radius_center_query(&ctx.project_id, &symbol_id);
                let center_rows = client.query(&query, Some(params))?;
                let center_node = center_rows
                    .first()
                    .and_then(|row| GraphNode::from_row(row, "function"))
                    .unwrap_or_else(|| GraphNode::new(&symbol_id, &symbol_id, "function"));

                let query = blast_radius_query(depth, limit);
                let params =
                    typed_query::string_params(&[("project", &ctx.project_id), ("id", &symbol_id)]);
                (symbol_id, center_node, client.query(&query, Some(params))?)
            }
            GraphBlastRadiusTarget::FilePath(file_path) => {
                let mut rows = vec![];
                let (query, params) =
                    blast_radius_file_call_query(&ctx.project_id, &file_path, depth, limit);
                rows.extend(client.query(&query, Some(params))?);
                let (query, params) =
                    blast_radius_file_import_query(&ctx.project_id, &file_path, depth, limit);
                rows.extend(client.query(&query, Some(params))?);
                let rows = dedupe_limited_blast_rows(rows, limit);
                let mut center_node = GraphNode::new(&file_path, &file_path, "file");
                center_node.file_path = Some(file_path.clone());
                (file_path.clone(), center_node, rows)
            }
        };

        center_node.blast_distance = Some(0);
        let mut payload = GraphPayload::with_center(center_id.clone());
        payload.push_node(center_node);

        for row in rows {
            let Some(node_id) = row_string_owned(&row, &["node_id"]) else {
                continue;
            };
            let mut node = GraphNode::new(
                node_id.clone(),
                row_string_owned(&row, &["node_name"]).unwrap_or_else(|| node_id.clone()),
                row_string_owned(&row, &["node_type"]).unwrap_or_else(|| "function".to_string()),
            );
            node.kind = row_string_owned(&row, &["kind"]);
            node.file_path = row_string_owned(&row, &["file_path"]);
            node.line_start = row_usize(&row, &["line"]);
            node.blast_distance = row_usize(&row, &["distance"]);
            payload.push_node(node);

            let relation =
                row_string_owned(&row, &["rel_type"]).unwrap_or_else(|| "call".to_string());
            let mut link = GraphLink::new(
                node_id,
                &center_id,
                if relation == "call" {
                    "CALLS"
                } else {
                    "IMPORTS"
                },
            );
            link.distance = row_usize(&row, &["distance"]);
            link.metadata = row_to_projection_metadata(&row);
            payload.links.push(link);
        }

        Ok(payload)
    })
}

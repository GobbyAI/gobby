use std::cmp::Reverse;

use gobby_core::falkor::Row;

use crate::config::Context;
use crate::graph::typed_query;
use crate::visibility::graph_versions::{admitted_namespaces, with_admitted_versions};

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

/// Overview order for files: most importers, then most symbols, then path.
fn overview_file_order(row: &Row) -> (Reverse<usize>, Reverse<usize>, Option<String>) {
    (
        Reverse(row_usize(row, &["import_count"]).unwrap_or(0)),
        Reverse(row_usize(row, &["symbol_count"]).unwrap_or(0)),
        row_string_owned(row, &["id"]),
    )
}

pub fn project_overview_graph(ctx: &Context, limit: usize) -> anyhow::Result<GraphPayload> {
    with_optional_core_graph(ctx, GraphPayload::default, |client| {
        let namespaces = admitted_namespaces(ctx)?;
        let limit = clamp_limit(limit);
        let link_limit = clamp_limit(limit.saturating_mul(4));
        let max_nodes = limit.saturating_mul(8);

        // Edges never cross namespaces, so each namespace's top files are a superset
        // of its contribution to the global top files.
        let mut files = Vec::new();
        for (owner, namespace) in namespaces.iter().enumerate() {
            let (query, params) = with_admitted_versions(
                project_overview_files_query(&namespace.project_id, limit),
                &namespace.versions,
            );
            for row in client.query(&query, Some(params))? {
                files.push((owner, row));
            }
        }
        files.sort_by(|(_, left), (_, right)| {
            overview_file_order(left).cmp(&overview_file_order(right))
        });
        files.truncate(limit);

        let mut payload = GraphPayload::default();
        for (_, row) in &files {
            add_node_from_row(&mut payload, row, "file");
        }

        for (owner, namespace) in namespaces.iter().enumerate() {
            let file_paths = files
                .iter()
                .filter(|(file_owner, _)| *file_owner == owner)
                .filter_map(|(_, row)| row_string_owned(row, &["id"]))
                .collect::<Vec<_>>();
            if file_paths.is_empty() {
                continue;
            }

            let (query, params) = with_admitted_versions(
                project_overview_imports_query(&namespace.project_id, &file_paths, link_limit),
                &namespace.versions,
            );
            for row in client.query(&query, Some(params))? {
                add_link_from_row(&mut payload, &row);
                if let Some(module_id) = row_string_owned(&row, &["target"]) {
                    payload.push_node(GraphNode::new(module_id.clone(), module_id, "module"));
                }
                if payload.node_count() >= max_nodes {
                    break;
                }
            }

            let (query, params) = with_admitted_versions(
                project_overview_defines_query(&namespace.project_id, &file_paths, link_limit),
                &namespace.versions,
            );
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

            let (query, params) = with_admitted_versions(
                project_overview_calls_query(&namespace.project_id, &file_paths, link_limit),
                &namespace.versions,
            );
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

        // A path is admitted in one namespace only, so the other namespaces return nothing.
        for namespace in admitted_namespaces(ctx)? {
            let (query, params) = with_admitted_versions(
                file_symbols_query(&namespace.project_id, file_path),
                &namespace.versions,
            );
            for row in client.query(&query, Some(params))? {
                add_node_from_row(&mut payload, &row, "function");
                if let Some(symbol_id) = row_string_owned(&row, &["id"]) {
                    let mut link = GraphLink::new(file_path, symbol_id, "DEFINES");
                    link.metadata = row_to_projection_metadata(&row);
                    payload.links.push(link);
                }
            }

            let (query, params) = with_admitted_versions(
                file_calls_query(&namespace.project_id, file_path),
                &namespace.versions,
            );
            for row in client.query(&query, Some(params))? {
                add_prefixed_node_from_row(&mut payload, &row, "source", "function");
                add_prefixed_node_from_row(&mut payload, &row, "target", "unresolved");
                add_link_from_row(&mut payload, &row);
            }
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
        // A symbol id names one namespace's node, so only its owner returns rows.
        let mut center = None;
        let mut rows: Vec<Row> = Vec::new();
        for namespace in admitted_namespaces(ctx)? {
            let (query, params) = with_admitted_versions(
                blast_radius_center_query(&namespace.project_id, symbol_id),
                &namespace.versions,
            );
            let center_rows = client.query(&query, Some(params))?;
            if center.is_none() {
                center = center_rows
                    .first()
                    .and_then(|row| GraphNode::from_row(row, "function"));
            }

            let (query, params) = with_admitted_versions(
                symbol_neighbors_query(&namespace.project_id, symbol_id, limit),
                &namespace.versions,
            );
            rows.extend(client.query(&query, Some(params))?);
        }

        let mut payload = GraphPayload::with_center(symbol_id.to_string());
        let center_node =
            center.unwrap_or_else(|| GraphNode::new(symbol_id, symbol_id, "function"));
        payload.push_node(center_node);

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
        let namespaces = admitted_namespaces(ctx)?;
        let (center_id, mut center_node, rows) = match target {
            GraphBlastRadiusTarget::SymbolId(symbol_id) => {
                // A symbol id names one namespace's node, so only its owner returns rows.
                let mut center = None;
                let mut rows = Vec::new();
                for namespace in &namespaces {
                    let (query, params) = with_admitted_versions(
                        blast_radius_center_query(&namespace.project_id, &symbol_id),
                        &namespace.versions,
                    );
                    let center_rows = client.query(&query, Some(params))?;
                    if center.is_none() {
                        center = center_rows
                            .first()
                            .and_then(|row| GraphNode::from_row(row, "function"));
                    }

                    let params = typed_query::string_params(&[
                        ("project", &namespace.project_id),
                        ("id", &symbol_id),
                    ]);
                    let (query, params) = with_admitted_versions(
                        (blast_radius_query(depth, limit), params),
                        &namespace.versions,
                    );
                    rows.extend(client.query(&query, Some(params))?);
                }
                let center_node =
                    center.unwrap_or_else(|| GraphNode::new(&symbol_id, &symbol_id, "function"));
                (symbol_id, center_node, rows)
            }
            GraphBlastRadiusTarget::FilePath(file_path) => {
                // A path is admitted in one namespace only, so the other namespaces return nothing.
                let mut rows = vec![];
                for namespace in &namespaces {
                    let (query, params) = with_admitted_versions(
                        blast_radius_file_call_query(
                            &namespace.project_id,
                            &file_path,
                            depth,
                            limit,
                        ),
                        &namespace.versions,
                    );
                    rows.extend(client.query(&query, Some(params))?);
                    let (query, params) = with_admitted_versions(
                        blast_radius_file_import_query(
                            &namespace.project_id,
                            &file_path,
                            depth,
                            limit,
                        ),
                        &namespace.versions,
                    );
                    rows.extend(client.query(&query, Some(params))?);
                }
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

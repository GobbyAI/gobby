//! Falkor hop fetch and `gcode graph view --view=class-hierarchy` entry.

use std::collections::HashMap;

use anyhow::Context as _;
use gobby_core::degradation::ServiceState;
use gobby_core::falkor::Row;
use serde_json::Value;

use crate::cli::GraphViewArgs;
use crate::codewiki_facts::{CodewikiFacts, GraphAvailability, MAX_DECLARED_EDGE_LIMIT};
use crate::config::Context;
use crate::graph::code_graph::GraphReadError;
use crate::graph::typed_query;
use crate::output::Format;
use crate::search::fts::ResolvedGraphSymbol;

use super::super::render::{build_view_payload, print_view};
use super::super::{
    CandidateEndpoint, endpoint_kind_from_label, hint_for_availability, local_machine_id,
    non_empty, symbol_seed, visible_map_for_candidates,
};
use super::{HeritageCursor, HeritageDirection, HeritageHopRow, exhaust_heritage_hop, walk_chg};

const FRONTIER_CHUNK_LEN: usize = 64;

/// One heritage hop, anchored on the frontier side through the `CodeSymbol`
/// id index (`UNWIND` + labelled id match) so FalkorDB never scans the graph;
/// the far endpoint may be a symbol, external symbol, or unresolved callee.
pub(crate) fn heritage_hop_query(
    project_id: &str,
    direction: HeritageDirection,
    frontier: &[String],
    after: Option<&HeritageCursor>,
    limit: usize,
) -> (String, HashMap<String, String>) {
    let listed = typed_query::id_list_literal(frontier);
    let anchor = "CodeSymbol {id: anchor_id, project: $project}";
    let (source_node, target_node, far_pred) = match direction {
        HeritageDirection::Ancestors => (
            format!("source:{anchor}"),
            "target {project: $project}".to_string(),
            "(target:CodeSymbol OR target:ExternalSymbol OR target:UnresolvedCallee)",
        ),
        HeritageDirection::Descendants => (
            "source {project: $project}".to_string(),
            format!("target:{anchor}"),
            "(source:CodeSymbol OR source:ExternalSymbol OR source:UnresolvedCallee)",
        ),
    };
    let mut predicates = vec![far_pred.to_string()];
    if after.is_some() {
        predicates.push(
            "(source.id > $after_source OR \
             (source.id = $after_source AND target.id > $after_target) OR \
             (source.id = $after_source AND target.id = $after_target AND type(r) > $after_rel) OR \
             (source.id = $after_source AND target.id = $after_target AND type(r) = $after_rel \
              AND id(r) > $after_edge))"
                .to_string(),
        );
    }
    let query = format!(
        "UNWIND [{listed}] AS anchor_id \
         MATCH ({source_node})-[r:INHERITS|EXTENDS|IMPLEMENTS]->({target_node}) \
         WHERE {where_clause} \
         RETURN source.id AS source, target.id AS target, type(r) AS rel, id(r) AS edge_id, \
                coalesce(source.name, source.id) AS source_name, \
                coalesce(target.name, target.id) AS target_name, \
                coalesce(source.file_path, '') AS source_file, \
                coalesce(target.file_path, '') AS target_file, \
                CASE WHEN source:ExternalSymbol THEN 'external' \
                     WHEN source:UnresolvedCallee THEN 'unresolved' \
                     ELSE 'symbol' END AS source_kind, \
                CASE WHEN target:ExternalSymbol THEN 'external' \
                     WHEN target:UnresolvedCallee THEN 'unresolved' \
                     ELSE 'symbol' END AS target_kind, \
                coalesce(r.file, source.file_path, '') AS owner_path, \
                coalesce(r.content_hash, '') AS owner_hash \
         ORDER BY source, target, rel, edge_id LIMIT {limit}",
        where_clause = predicates.join(" AND "),
    );
    let mut params = typed_query::string_params(&[("project", project_id)]);
    if let Some(cursor) = after {
        params.insert(
            "after_source".to_string(),
            typed_query::cypher_string_literal(&cursor.source),
        );
        params.insert(
            "after_target".to_string(),
            typed_query::cypher_string_literal(&cursor.target),
        );
        params.insert(
            "after_rel".to_string(),
            typed_query::cypher_string_literal(&cursor.rel),
        );
        params.insert("after_edge".to_string(), cursor.edge_id.to_string());
    }
    (query, params)
}

fn row_str<'a>(row: &'a Row, key: &str) -> Option<&'a str> {
    row.get(key).and_then(Value::as_str)
}

fn row_i64(row: &Row, key: &str) -> Option<i64> {
    let value = row.get(key)?;
    value
        .as_i64()
        .or_else(|| value.as_u64().and_then(|n| i64::try_from(n).ok()))
        .or_else(|| value.as_str()?.parse().ok())
}

fn endpoint_from_row(
    row: &Row,
    id_key: &str,
    name_key: &str,
    file_key: &str,
    kind_key: &str,
) -> Option<CandidateEndpoint> {
    let id = row_str(row, id_key)?.to_string();
    let kind = endpoint_kind_from_label(row_str(row, kind_key).unwrap_or("symbol"));
    let name = row_str(row, name_key)
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| id.clone());
    Some(CandidateEndpoint {
        kind,
        id,
        name: Some(name),
        file: row_str(row, file_key).and_then(non_empty),
        content_hash: None,
        machine_id: None,
    })
}

fn rows_to_heritage(rows: &[Row], machine_id: &str) -> Vec<HeritageHopRow> {
    rows.iter()
        .filter_map(|row| {
            Some(HeritageHopRow {
                source: endpoint_from_row(
                    row,
                    "source",
                    "source_name",
                    "source_file",
                    "source_kind",
                )?,
                target: endpoint_from_row(
                    row,
                    "target",
                    "target_name",
                    "target_file",
                    "target_kind",
                )?,
                rel: row_str(row, "rel")?.to_string(),
                edge_id: row_i64(row, "edge_id")?,
                owner_path: row_str(row, "owner_path").unwrap_or("").to_string(),
                owner_hash: row_str(row, "owner_hash").unwrap_or("").to_string(),
                owner_machine: machine_id.to_string(),
                overlay_shadowed: false,
            })
        })
        .collect()
}

fn falkor_page(
    ctx: &Context,
    direction: HeritageDirection,
    frontier: &[String],
    after: Option<&HeritageCursor>,
) -> anyhow::Result<Vec<HeritageHopRow>> {
    let Some(config) = &ctx.falkordb else {
        return Err(anyhow::Error::new(GraphReadError::NotConfigured));
    };
    let (query, params) = heritage_hop_query(
        &ctx.project_id,
        direction,
        frontier,
        after,
        MAX_DECLARED_EDGE_LIMIT,
    );
    let connection_config = config.connection_config();
    match gobby_core::falkor::with_graph(
        Some(&connection_config),
        &config.graph_name,
        None,
        |client| client.query(&query, Some(params)).map(Some),
    ) {
        Ok((Some(rows), ServiceState::Available)) => {
            Ok(rows_to_heritage(&rows, &local_machine_id()))
        }
        Ok((_, ServiceState::NotConfigured)) => {
            Err(anyhow::Error::new(GraphReadError::NotConfigured))
        }
        Ok((_, ServiceState::Unreachable { message })) => {
            Err(anyhow::Error::new(GraphReadError::Unreachable { message }))
        }
        Ok((None, ServiceState::Available)) => {
            Err(anyhow::Error::new(GraphReadError::QueryFailed {
                message: "graph read returned no value".to_string(),
            }))
        }
        Err(error) => Err(anyhow::Error::new(GraphReadError::QueryFailed {
            message: format!("{error:#}"),
        })),
    }
}

fn fetch_chg_hop(
    ctx: &Context,
    frontier: &[CandidateEndpoint],
    direction: HeritageDirection,
) -> anyhow::Result<Vec<HeritageHopRow>> {
    if frontier.is_empty() {
        return Ok(Vec::new());
    }
    let mut all = Vec::new();
    for chunk in frontier.chunks(FRONTIER_CHUNK_LEN) {
        let ids = chunk
            .iter()
            .map(|endpoint| endpoint.id.clone())
            .collect::<Vec<_>>();
        let rows = exhaust_heritage_hop(
            |cursor| falkor_page(ctx, direction, &ids, cursor),
            MAX_DECLARED_EDGE_LIMIT,
        )?;
        all.extend(rows);
    }
    Ok(all)
}

pub(crate) fn run(
    ctx: &Context,
    args: &GraphViewArgs,
    symbol: &ResolvedGraphSymbol,
    format: Format,
) -> anyhow::Result<()> {
    let facts = CodewikiFacts::from_context(ctx.clone());
    let hint = hint_for_availability(ctx, &facts.graph_availability());
    let (seed, seed_endpoint) = symbol_seed(symbol);
    if !matches!(facts.graph_availability(), GraphAvailability::Available) {
        return print_view(
            &super::super::empty_view_payload(ctx, args, seed, hint)?,
            format,
        );
    }
    let walk = walk_chg(
        seed_endpoint,
        args.effective_depth(),
        |edges| visible_map_for_candidates(ctx, edges),
        |frontier, direction| fetch_chg_hop(ctx, frontier, direction),
    )?;
    let payload = build_view_payload(
        ctx.project_id.clone(),
        ctx.project_root.display().to_string(),
        args.view,
        seed,
        args.effective_depth(),
        walk.incoming_truncated,
        walk.outgoing_truncated,
        hint,
        walk.nodes,
        walk.edges,
        Vec::new(),
    )
    .context("build class-hierarchy view payload")?;
    print_view(&payload, format)
}

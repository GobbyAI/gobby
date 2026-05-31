use anyhow::Context as _;
use gobby_core::falkor::GraphClient;

use super::queries::{
    report_bridge_edges_query, report_code_edge_counts_query, report_hotspots_query,
    report_incoming_call_hotspots_query, report_node_counts_query, report_target_frequencies_query,
};
use super::rows::{
    row_to_bridge_edge_hypothesis, row_to_graph_hotspot, row_to_target_frequency,
    rows_to_named_counts,
};
use super::types::{
    BridgeEdgeInput, GraphHotspot, GraphReportHotspots, GraphReportSummary, ReportGraphSnapshot,
    TargetFrequency,
};

pub(super) fn load_report_snapshot(
    client: &mut GraphClient,
    project_id: &str,
    top_n: usize,
) -> anyhow::Result<ReportGraphSnapshot> {
    let (query, params) = report_node_counts_query(project_id);
    let node_counts_by_type = rows_to_named_counts(
        client
            .query(&query, Some(params))
            .context("load graph report node counts")?,
    );
    let node_count = node_counts_by_type.values().sum();

    let (query, params) = report_code_edge_counts_query(project_id);
    let code_edge_counts = rows_to_named_counts(
        client
            .query(&query, Some(params))
            .context("load graph report code edge counts")?,
    );
    let edge_count = code_edge_counts.values().sum();

    let summary = GraphReportSummary {
        node_count,
        edge_count,
        node_counts_by_type,
        code_edge_counts,
    };

    let hotspots = GraphReportHotspots {
        high_degree_files: load_hotspots(client, project_id, "file", top_n)?,
        high_degree_symbols: load_hotspots(client, project_id, "symbol", top_n)?,
        high_degree_modules: load_hotspots(client, project_id, "module", top_n)?,
        incoming_call_hotspots: load_incoming_call_hotspots(client, project_id, top_n)?,
    };

    let unresolved_targets = load_target_frequencies(client, project_id, "unresolved", top_n)?;
    let external_targets = load_target_frequencies(client, project_id, "external", top_n)?;

    let (query, params) = report_bridge_edges_query(project_id);
    let rows = client
        .query(&query, Some(params))
        .context("load graph report bridge edges")?;
    let bridge_edges = BridgeEdgeInput::available(
        rows.iter()
            .filter_map(row_to_bridge_edge_hypothesis)
            .collect(),
    );

    Ok(ReportGraphSnapshot {
        nodes: vec![],
        code_edges: vec![],
        summary: Some(summary),
        hotspots: Some(hotspots),
        unresolved_targets: Some(unresolved_targets),
        external_targets: Some(external_targets),
        bridge_edges,
    })
}

fn load_hotspots(
    client: &mut GraphClient,
    project_id: &str,
    node_class: &str,
    top_n: usize,
) -> anyhow::Result<Vec<GraphHotspot>> {
    let (query, params) = report_hotspots_query(project_id, node_class, top_n);
    Ok(client
        .query(&query, Some(params))
        .with_context(|| format!("load graph report {node_class} hotspots"))?
        .iter()
        .filter_map(row_to_graph_hotspot)
        .collect())
}

fn load_incoming_call_hotspots(
    client: &mut GraphClient,
    project_id: &str,
    top_n: usize,
) -> anyhow::Result<Vec<GraphHotspot>> {
    let (query, params) = report_incoming_call_hotspots_query(project_id, top_n);
    Ok(client
        .query(&query, Some(params))
        .context("load graph report incoming call hotspots")?
        .iter()
        .filter_map(row_to_graph_hotspot)
        .collect())
}

fn load_target_frequencies(
    client: &mut GraphClient,
    project_id: &str,
    target_type: &str,
    top_n: usize,
) -> anyhow::Result<Vec<TargetFrequency>> {
    let (query, params) = report_target_frequencies_query(project_id, target_type, top_n);
    Ok(client
        .query(&query, Some(params))
        .with_context(|| format!("load graph report {target_type} target frequencies"))?
        .iter()
        .filter_map(row_to_target_frequency)
        .collect())
}

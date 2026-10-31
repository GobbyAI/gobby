use std::cmp::Ordering;
use std::collections::BTreeMap;
use std::collections::btree_map::Entry;

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
    BridgeEdgeHypothesis, BridgeEdgeInput, CommunityInput, GraphHotspot, GraphReportHotspots,
    GraphReportSummary, ReportGraphSnapshot, TargetFrequency,
};
use crate::communities::{self, StoredCommunity};
use crate::config::Context;
use crate::visibility::graph_versions::{
    AdmittedGraphVersions, AdmittedNamespace, with_admitted_versions,
};
use gobby_core::falkor::Row;

/// The degradation input name for the stored import communities.
pub(super) const CODE_COMMUNITIES_INPUT: &str = "code_communities";

/// The degradation detail for a project with no stored import communities.
pub(super) const MISSING_COMMUNITIES_DETAIL: &str =
    "no stored import communities for this project; run `gcode index` to compute them";

/// Reads the stored import communities for the report. The report never
/// computes a partition itself.
pub(super) fn load_report_communities(ctx: &Context) -> CommunityInput {
    community_input(
        crate::db::connect_readonly(&ctx.database_url)
            .and_then(|mut conn| communities::read_for_context(&mut conn, ctx)),
    )
}

/// A failed read, or a project without stored rows, makes the input
/// unavailable; the report degrades instead of failing.
pub(super) fn community_input(rows: anyhow::Result<Vec<StoredCommunity>>) -> CommunityInput {
    match rows {
        Ok(rows) if rows.is_empty() => {
            CommunityInput::Unavailable(MISSING_COMMUNITIES_DETAIL.to_string())
        }
        Ok(rows) => CommunityInput::Available(rows),
        Err(error) => CommunityInput::Unavailable(format!("{error:#}")),
    }
}

/// Loads the report from every admitted namespace and merges the rows. Node and
/// edge ids are namespace-local, so each namespace is read on its own.
pub(super) fn load_report_snapshot(
    client: &mut GraphClient,
    namespaces: &[AdmittedNamespace],
    top_n: usize,
) -> anyhow::Result<ReportGraphSnapshot> {
    if let Ok(chunks) = std::env::var("SPIKE_CHUNK_ORACLE") { // SPIKE-PARITY
        for namespace in namespaces.iter().filter(|ns| std::env::var("SPIKE_CHUNK_ONLY").map_or(true, |only| only == ns.project_id)) { // SPIKE-PARITY
            super::spike_oracle::run(client, namespace, chunks.parse()?)?; // SPIKE-PARITY
        } // SPIKE-PARITY
        std::process::exit(0); // SPIKE-PARITY
    } // SPIKE-PARITY
    if let Ok(probe) = std::env::var("SPIKE_PROBE_FILE") { // SPIKE-TIMING
        let text = std::fs::read_to_string(&probe)?; // SPIKE-TIMING
        for namespace in namespaces { // SPIKE-TIMING
            for (idx, query) in text.split(";;").map(str::trim).filter(|q| !q.is_empty()).enumerate() { // SPIKE-TIMING
                let params = crate::graph::typed_query::string_params(&[("project", namespace.project_id.as_str())]); // SPIKE-TIMING
                let (query, params) = with_admitted_versions((query.to_string(), params), &namespace.versions); // SPIKE-TIMING
                let started = std::time::Instant::now(); // SPIKE-TIMING
                match client.query(&query, Some(params)) { // SPIKE-TIMING
                    Ok(rows) => eprintln!("SPIKE probe ns={} q={idx} rows={} {:?} all={:?}", namespace.project_id, rows.len(), started.elapsed(), rows), // SPIKE-TIMING
                    Err(error) => eprintln!("SPIKE probe ns={} q={idx} error={error:#} {:?}", namespace.project_id, started.elapsed()), // SPIKE-TIMING
                } // SPIKE-TIMING
            } // SPIKE-TIMING
        } // SPIKE-TIMING
        std::process::exit(0); // SPIKE-TIMING
    } // SPIKE-TIMING
    let mut parts = Vec::with_capacity(namespaces.len());
    for namespace in namespaces {
        parts.push(load_namespace_rows(client, namespace, top_n)?);
    }
    Ok(merge_namespace_rows(&parts, top_n))
}

/// One namespace's share of the report, ranked within the namespace.
#[derive(Default)]
struct NamespaceRows {
    node_counts_by_type: BTreeMap<String, usize>,
    code_edge_counts: BTreeMap<String, usize>,
    hotspots: GraphReportHotspots,
    unresolved_targets: Vec<TargetFrequency>,
    external_targets: Vec<TargetFrequency>,
    bridge_edges: Vec<BridgeEdgeHypothesis>,
}

fn load_namespace_rows(
    client: &mut GraphClient,
    namespace: &AdmittedNamespace,
    top_n: usize,
) -> anyhow::Result<NamespaceRows> {
    let project_id = namespace.project_id.as_str();
    let admitted = &namespace.versions;

    let started = std::time::Instant::now(); // SPIKE-TIMING
    let (query, params) = with_admitted_versions(super::queries::report_node_counts_query(project_id), admitted);
    let node_counts_by_type = rows_to_named_counts(
        client
            .query(&query, Some(params))
            .context("load graph report node counts")?,
    );
    eprintln!("SPIKE node_counts {:?} {:?}", node_counts_by_type, started.elapsed()); // SPIKE-TIMING

    let started = std::time::Instant::now(); // SPIKE-TIMING
    let (query, params) =
        with_admitted_versions(super::queries::report_code_edge_counts_query(project_id), admitted);
    let code_edge_counts = rows_to_named_counts(
        client
            .query(&query, Some(params))
            .context("load graph report code edge counts")?,
    );
    eprintln!("SPIKE code_edge_counts {:?} {:?}", code_edge_counts, started.elapsed()); // SPIKE-TIMING

    // Files, symbols, and external or unresolved targets never repeat across
    // namespaces, so a namespace's top rows cover its share of the global top
    // rows. Module names do repeat, and one module can gain degree in several
    // namespaces, so module rows stay unlimited until the merge ranks them.
    let started = std::time::Instant::now(); // SPIKE-TIMING
    let skip_hotspots = std::env::var_os("SPIKE_SKIP_HOTSPOTS").is_some(); // SPIKE-TIMING
    let high_degree_files = if skip_hotspots { Vec::new() } else { load_hotspots(client, project_id, admitted, "file", Some(top_n))? }; // SPIKE-TIMING
    eprintln!("SPIKE hot file {:?}", started.elapsed()); // SPIKE-TIMING
    let started = std::time::Instant::now(); // SPIKE-TIMING
    let high_degree_symbols = if skip_hotspots { Vec::new() } else { load_hotspots(client, project_id, admitted, "symbol", Some(top_n))? }; // SPIKE-TIMING
    eprintln!("SPIKE hot symbol {:?}", started.elapsed()); // SPIKE-TIMING
    let started = std::time::Instant::now(); // SPIKE-TIMING
    let high_degree_modules = if skip_hotspots { Vec::new() } else { load_hotspots(client, project_id, admitted, "module", None)? }; // SPIKE-TIMING
    eprintln!("SPIKE hot module {:?}", started.elapsed()); // SPIKE-TIMING
    let started = std::time::Instant::now(); // SPIKE-TIMING
    let incoming_call_hotspots = load_incoming_call_hotspots(client, project_id, admitted, top_n)?;
    eprintln!("SPIKE incoming {:?}", started.elapsed()); // SPIKE-TIMING
    let hotspots = GraphReportHotspots {
        high_degree_files,
        high_degree_symbols,
        high_degree_modules,
        incoming_call_hotspots,
    };

    let started = std::time::Instant::now(); // SPIKE-TIMING
    let unresolved_targets =
        load_target_frequencies(client, project_id, admitted, "unresolved", top_n)?;
    eprintln!("SPIKE targets unresolved {:?}", started.elapsed()); // SPIKE-TIMING
    let started = std::time::Instant::now(); // SPIKE-TIMING
    let external_targets =
        load_target_frequencies(client, project_id, admitted, "external", top_n)?;
    eprintln!("SPIKE targets external {:?}", started.elapsed()); // SPIKE-TIMING

    let (query, params) = with_admitted_versions(super::queries::report_bridge_edges_query(project_id), admitted);
    let rows = client
        .query(&query, Some(params))
        .context("load graph report bridge edges")?;
    let bridge_edges = collect_report_rows(&rows, "bridge edges", row_to_bridge_edge_hypothesis);

    Ok(NamespaceRows {
        node_counts_by_type,
        code_edge_counts,
        hotspots,
        unresolved_targets,
        external_targets,
        bridge_edges,
    })
}

fn merge_namespace_rows(parts: &[NamespaceRows], top_n: usize) -> ReportGraphSnapshot {
    let node_counts_by_type = sum_counts(parts.iter().map(|part| &part.node_counts_by_type));
    let code_edge_counts = sum_counts(parts.iter().map(|part| &part.code_edge_counts));
    let node_count: usize = node_counts_by_type.values().sum();
    let edge_count: usize = code_edge_counts.values().sum();

    let hotspots = GraphReportHotspots {
        high_degree_files: merge_hotspots(
            parts
                .iter()
                .map(|part| part.hotspots.high_degree_files.as_slice()),
            top_n,
        ),
        high_degree_symbols: merge_hotspots(
            parts
                .iter()
                .map(|part| part.hotspots.high_degree_symbols.as_slice()),
            top_n,
        ),
        high_degree_modules: merge_hotspots(
            parts
                .iter()
                .map(|part| part.hotspots.high_degree_modules.as_slice()),
            top_n,
        ),
        incoming_call_hotspots: merge_hotspots(
            parts
                .iter()
                .map(|part| part.hotspots.incoming_call_hotspots.as_slice()),
            top_n,
        ),
    };
    let unresolved_targets = merge_targets(
        parts.iter().map(|part| part.unresolved_targets.as_slice()),
        top_n,
    );
    let external_targets = merge_targets(
        parts.iter().map(|part| part.external_targets.as_slice()),
        top_n,
    );
    let bridge_edges = parts
        .iter()
        .flat_map(|part| part.bridge_edges.iter().cloned())
        .collect();

    // ReportGraphSnapshot is metadata-only here: summary, hotspots,
    // unresolved_targets, external_targets, and bridge_edges are loaded in this
    // step, while full nodes and code_edges are populated separately.
    ReportGraphSnapshot {
        nodes: vec![],
        code_edges: vec![],
        summary: Some(GraphReportSummary {
            node_count,
            edge_count,
            node_counts_by_type,
            code_edge_counts,
        }),
        hotspots: Some(hotspots),
        unresolved_targets: Some(unresolved_targets),
        external_targets: Some(external_targets),
        bridge_edges: BridgeEdgeInput::available(bridge_edges),
    }
}

fn sum_counts<'a>(
    counts: impl IntoIterator<Item = &'a BTreeMap<String, usize>>,
) -> BTreeMap<String, usize> {
    let mut total: BTreeMap<String, usize> = BTreeMap::new();
    for map in counts {
        for (name, count) in map {
            *total.entry(name.clone()).or_insert(0) += count;
        }
    }
    total
}

fn merge_hotspots<'a>(
    lists: impl IntoIterator<Item = &'a [GraphHotspot]>,
    top_n: usize,
) -> Vec<GraphHotspot> {
    merge_ranked(
        lists,
        |row| row.id.as_str(),
        |merged, row| {
            merged.degree += row.degree;
            merged.incoming += row.incoming;
            merged.outgoing += row.outgoing;
        },
        |left, right| {
            right
                .degree
                .cmp(&left.degree)
                .then_with(|| left.name.cmp(&right.name))
                .then_with(|| left.id.cmp(&right.id))
        },
        top_n,
    )
}

fn merge_targets<'a>(
    lists: impl IntoIterator<Item = &'a [TargetFrequency]>,
    top_n: usize,
) -> Vec<TargetFrequency> {
    merge_ranked(
        lists,
        |row| row.id.as_str(),
        |merged, row| merged.count += row.count,
        |left, right| {
            right
                .count
                .cmp(&left.count)
                .then_with(|| left.name.cmp(&right.name))
                .then_with(|| left.id.cmp(&right.id))
        },
        top_n,
    )
}

/// Combines rows that share an id, ranks the result, and keeps the first `top_n`.
fn merge_ranked<'a, T: Clone + 'a>(
    lists: impl IntoIterator<Item = &'a [T]>,
    id: impl Fn(&T) -> &str,
    combine: impl Fn(&mut T, &T),
    order: impl Fn(&T, &T) -> Ordering,
    top_n: usize,
) -> Vec<T> {
    let mut by_id = BTreeMap::<String, T>::new();
    for row in lists.into_iter().flatten() {
        match by_id.entry(id(row).to_string()) {
            Entry::Occupied(mut entry) => combine(entry.get_mut(), row),
            Entry::Vacant(entry) => {
                entry.insert(row.clone());
            }
        }
    }
    let mut merged: Vec<T> = by_id.into_values().collect();
    merged.sort_by(order);
    merged.truncate(top_n);
    merged
}

#[cfg(test)]
#[path = "loading/tests.rs"]
mod tests;

fn load_hotspots(
    client: &mut GraphClient,
    project_id: &str,
    admitted: &AdmittedGraphVersions,
    node_class: &str,
    limit: Option<usize>,
) -> anyhow::Result<Vec<GraphHotspot>> {
    let (query, params) = with_admitted_versions(
        (if spike_head() { super::head_queries::report_hotspots_query(project_id, node_class, limit) } else { super::queries::report_hotspots_query(project_id, node_class, limit) }),
        admitted,
    );
    let rows = client
        .query(&query, Some(params))
        .with_context(|| format!("load graph report {node_class} hotspots"))?;
    Ok(collect_report_rows(
        &rows,
        &format!("{node_class} hotspots"),
        row_to_graph_hotspot,
    ))
}

fn load_incoming_call_hotspots(
    client: &mut GraphClient,
    project_id: &str,
    admitted: &AdmittedGraphVersions,
    top_n: usize,
) -> anyhow::Result<Vec<GraphHotspot>> {
    let (query, params) = with_admitted_versions(
        (if spike_head() { super::head_queries::report_incoming_call_hotspots_query(project_id, top_n) } else { super::queries::report_incoming_call_hotspots_query(project_id, top_n) }),
        admitted,
    );
    let rows = client
        .query(&query, Some(params))
        .context("load graph report incoming call hotspots")?;
    Ok(collect_report_rows(
        &rows,
        "incoming call hotspots",
        row_to_graph_hotspot,
    ))
}

fn load_target_frequencies(
    client: &mut GraphClient,
    project_id: &str,
    admitted: &AdmittedGraphVersions,
    target_type: &str,
    top_n: usize,
) -> anyhow::Result<Vec<TargetFrequency>> {
    let (query, params) = with_admitted_versions(
        (if spike_head() { super::head_queries::report_target_frequencies_query(project_id, target_type, top_n) } else { super::queries::report_target_frequencies_query(project_id, target_type, top_n) }),
        admitted,
    );
    let rows = client
        .query(&query, Some(params))
        .with_context(|| format!("load graph report {target_type} target frequencies"))?;
    Ok(collect_report_rows(
        &rows,
        &format!("{target_type} target frequencies"),
        row_to_target_frequency,
    ))
}

fn collect_report_rows<T>(rows: &[Row], label: &str, mapper: impl Fn(&Row) -> Option<T>) -> Vec<T> {
    let mut dropped = 0usize;
    let values = rows
        .iter()
        .filter_map(|row| {
            let value = mapper(row);
            if value.is_none() {
                dropped += 1;
            }
            value
        })
        .collect::<Vec<_>>();
    if dropped > 0 {
        log::warn!("dropped {dropped} malformed graph report row(s) while loading {label}");
    }
    values
}

// SPIKE-PARITY
fn spike_head() -> bool { std::env::var_os("SPIKE_HEAD").is_some() }

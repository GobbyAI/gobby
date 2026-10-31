use std::cmp::Ordering;
use std::collections::BTreeMap;
use std::collections::btree_map::Entry;

use anyhow::Context as _;
use gobby_core::falkor::GraphClient;

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

    let (query, params) = with_admitted_versions(
        super::queries::report_node_counts_query(project_id),
        admitted,
    );
    let node_counts_by_type = rows_to_named_counts(
        client
            .query(&query, Some(params))
            .context("load graph report node counts")?,
    );

    let (query, params) = with_admitted_versions(
        super::queries::report_code_edge_counts_query(project_id),
        admitted,
    );
    let code_edge_counts = rows_to_named_counts(
        client
            .query(&query, Some(params))
            .context("load graph report code edge counts")?,
    );

    // Files, symbols, and external or unresolved targets never repeat across
    // namespaces, so a namespace's top rows cover its share of the global top
    // rows. Module names do repeat, and one module can gain degree in several
    // namespaces, so module rows stay unlimited until the merge ranks them.
    let high_degree_files = load_hotspots(client, project_id, admitted, "file", Some(top_n))?;
    let high_degree_symbols = load_hotspots(client, project_id, admitted, "symbol", Some(top_n))?;
    let high_degree_modules = load_hotspots(client, project_id, admitted, "module", None)?;
    let incoming_call_hotspots = load_incoming_call_hotspots(client, project_id, admitted, top_n)?;
    let hotspots = GraphReportHotspots {
        high_degree_files,
        high_degree_symbols,
        high_degree_modules,
        incoming_call_hotspots,
    };

    let unresolved_targets =
        load_target_frequencies(client, project_id, admitted, "unresolved", top_n)?;
    let external_targets =
        load_target_frequencies(client, project_id, admitted, "external", top_n)?;

    let (base, base_params) = super::queries::report_bridge_edges_query(project_id);
    let rows = read_to_completion(|skip, page| {
        let (query, params) = with_admitted_versions(
            (window_query(&base, skip, page), base_params.clone()),
            admitted,
        );
        client
            .query(&query, Some(params))
            .context("load graph report bridge edges")
    })?;
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
    let mut run = |built| {
        let (query, params) = with_admitted_versions(built, admitted);
        client
            .query(&query, Some(params))
            .with_context(|| format!("load graph report {node_class} hotspots"))
    };
    let rows = match limit {
        Some(top_n) => run(super::queries::report_hotspots_query(
            project_id,
            node_class,
            Some(top_n),
        ))?,
        None => {
            let (base, params) =
                super::queries::report_hotspots_query(project_id, node_class, None);
            read_to_completion(|skip, page| run((window_query(&base, skip, page), params.clone())))?
        }
    };
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
        super::queries::report_incoming_call_hotspots_query(project_id, top_n),
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
        super::queries::report_target_frequencies_query(project_id, target_type, top_n),
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

/// Engine result cap (RESULTSET_SIZE). A read past it truncates silently.
pub(super) const ENGINE_RESULT_CAP: usize = 10_000;

/// Reads windows until one comes back short. `page(skip, limit)` returns the rows
/// after `skip`, at most `limit`; a window that fills the cap may have more behind it.
pub(super) fn read_to_completion<T>(
    mut page: impl FnMut(usize, usize) -> anyhow::Result<Vec<T>>,
) -> anyhow::Result<Vec<T>> {
    let mut rows = Vec::new();
    loop {
        let batch = page(rows.len(), ENGINE_RESULT_CAP)?;
        let full = batch.len() == ENGINE_RESULT_CAP;
        rows.extend(batch);
        if !full {
            return Ok(rows);
        }
    }
}

/// Appends one window to a builder's ordered query. Builders with a total ORDER BY
/// and no LIMIT of their own page cleanly; the window is appended after the sort.
fn window_query(query: &str, skip: usize, limit: usize) -> String {
    format!("{query} SKIP {skip} LIMIT {limit}")
}

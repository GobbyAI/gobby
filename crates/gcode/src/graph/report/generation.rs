use std::collections::HashMap;

use gobby_core::degradation::ServiceState;

use crate::config::Context;
use crate::visibility::graph_versions::admitted_namespaces;

#[cfg(test)]
use super::RELATES_TO_CODE;
use super::loading::{CODE_COMMUNITIES_INPUT, load_report_communities, load_report_snapshot};
use super::render::{RenderMarkdownInput, render_markdown};
use super::summary::{
    normalize_bridge_edges, suggested_questions, summarize_bridge_edges, summarize_graph,
    summarize_hotspots, target_frequencies,
};
use super::time::now_iso8601;
use super::types::{
    BridgeEdgeInput, CommunityInput, GraphReportCommunities, GraphReportHotspots,
    ProjectGraphReport, ProjectGraphReportError, ProjectGraphReportOptions, ReportDegradation,
    ReportGraphSnapshot,
};

#[cfg(test)]
pub fn generate_report(ctx: &Context) -> Result<ProjectGraphReport, ProjectGraphReportError> {
    generate_report_with_options(ctx, ProjectGraphReportOptions::default())
}

pub fn generate_report_with_options(
    ctx: &Context,
    options: ProjectGraphReportOptions,
) -> Result<ProjectGraphReport, ProjectGraphReportError> {
    let Some(config) = ctx.falkordb.as_ref() else {
        return Err(ProjectGraphReportError::GraphServiceNotConfigured);
    };

    let t0 = std::time::Instant::now(); // SPIKE-TIMING
    eprintln!("SPIKE pre entry_epoch {:.3}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())); // SPIKE-TIMING
    let connection_config = config.connection_config();
    let options = options.normalized();
    let communities = load_report_communities(ctx);
    eprintln!("SPIKE pre communities {:?}", t0.elapsed()); // SPIKE-TIMING
    let result = gobby_core::falkor::with_graph(
        Some(&connection_config),
        &config.graph_name,
        ReportGraphSnapshot::default(),
        |client| {
            eprintln!("SPIKE pre closure_entry {:?}", t0.elapsed()); // SPIKE-TIMING
            let started = std::time::Instant::now(); // SPIKE-TIMING
            let namespaces = admitted_namespaces(ctx)?;
            eprintln!("SPIKE pre admitted {:?} namespaces={}", started.elapsed(), namespaces.len()); // SPIKE-TIMING
            if std::env::var_os("SPIKE_PRE_ONLY").is_some() { // SPIKE-TIMING
                eprintln!("SPIKE pre exit_after_admitted total {:?}", t0.elapsed()); // SPIKE-TIMING
                std::process::exit(0); // SPIKE-TIMING
            } // SPIKE-TIMING
            load_report_snapshot(client, &namespaces, options.top_n)
        },
    );
    eprintln!("SPIKE pre with_graph_returned {:?}", t0.elapsed()); // SPIKE-TIMING

    match result {
        Ok((snapshot, ServiceState::Available)) => {
            eprintln!("SPIKE pre snapshot_ready {:?}", t0.elapsed()); // SPIKE-TIMING
            let report = generate_report_from_snapshot_with_options(
                &ctx.project_id,
                now_iso8601(),
                snapshot,
                communities,
                options,
            );
            eprintln!("SPIKE pre render_done {:?}", t0.elapsed()); // SPIKE-TIMING
            Ok(report)
        }
        Ok((_, ServiceState::NotConfigured)) => {
            Err(ProjectGraphReportError::GraphServiceNotConfigured)
        }
        Ok((_, ServiceState::Unreachable { message })) => {
            Err(ProjectGraphReportError::GraphServiceUnreachable { message })
        }
        Err(error) => Err(ProjectGraphReportError::GraphQueryFailed {
            message: format!("{error:#}"),
        }),
    }
}

#[cfg(test)]
pub fn empty_report(project_id: impl Into<String>) -> ProjectGraphReport {
    generate_report_from_snapshot(project_id, now_iso8601(), ReportGraphSnapshot::default())
}

#[cfg(test)]
pub(super) fn generate_report_from_snapshot(
    project_id: impl Into<String>,
    generated_at: impl Into<String>,
    snapshot: ReportGraphSnapshot,
) -> ProjectGraphReport {
    generate_report_from_snapshot_with_options(
        project_id,
        generated_at,
        snapshot,
        CommunityInput::default(),
        ProjectGraphReportOptions::default().normalized(),
    )
}

pub(super) fn generate_report_from_snapshot_with_options(
    project_id: impl Into<String>,
    generated_at: impl Into<String>,
    snapshot: ReportGraphSnapshot,
    communities: CommunityInput,
    options: ProjectGraphReportOptions,
) -> ProjectGraphReport {
    let project_id = project_id.into();
    let generated_at = generated_at.into();
    let node_by_id = snapshot
        .nodes
        .iter()
        .map(|node| (node.id.as_str(), node))
        .collect::<HashMap<_, _>>();

    let summary = snapshot
        .summary
        .clone()
        .unwrap_or_else(|| summarize_graph(&snapshot.nodes, &snapshot.code_edges));
    let (hotspots, hotspot_degradation) = match snapshot.hotspots.clone() {
        Some(hotspots) => (hotspots, None),
        None => match summarize_hotspots(&snapshot.nodes, &snapshot.code_edges, options.top_n) {
            Ok(hotspots) => (hotspots, None),
            Err(degradation) => (GraphReportHotspots::default(), Some(degradation)),
        },
    };
    let unresolved_targets = snapshot.unresolved_targets.clone().unwrap_or_else(|| {
        target_frequencies(
            &snapshot.code_edges,
            &node_by_id,
            "unresolved",
            options.top_n,
        )
    });
    let external_targets = snapshot.external_targets.clone().unwrap_or_else(|| {
        target_frequencies(&snapshot.code_edges, &node_by_id, "external", options.top_n)
    });

    let (bridge_edges, mut degradation_details): (_, Vec<ReportDegradation>) =
        match snapshot.bridge_edges {
            BridgeEdgeInput::Available(edges) => (normalize_bridge_edges(edges), vec![]),
            #[cfg(test)]
            BridgeEdgeInput::Unavailable(reason) => (
                vec![],
                vec![ReportDegradation {
                    input: RELATES_TO_CODE.to_string(),
                    required: false,
                    detail: reason,
                }],
            ),
        };
    let bridge_summary = summarize_bridge_edges(&bridge_edges);
    degradation_details.extend(hotspot_degradation);
    let communities = match communities {
        CommunityInput::Available(rows) => {
            Some(GraphReportCommunities::from_rows(&rows, options.top_n))
        }
        CommunityInput::Unavailable(detail) => {
            degradation_details.push(ReportDegradation {
                input: CODE_COMMUNITIES_INPUT.to_string(),
                required: false,
                detail,
            });
            None
        }
    };
    degradation_details.sort_by(|left, right| left.input.cmp(&right.input));

    let suggested_investigation_questions = suggested_questions(
        &hotspots,
        &unresolved_targets,
        &external_targets,
        bridge_summary.as_ref(),
        &degradation_details,
    );
    let markdown = render_markdown(RenderMarkdownInput {
        project_id: &project_id,
        generated_at: &generated_at,
        summary: &summary,
        hotspots: &hotspots,
        unresolved_targets: &unresolved_targets,
        external_targets: &external_targets,
        communities: communities.as_ref(),
        bridge_summary: bridge_summary.as_ref(),
        degradation_details: &degradation_details,
        top_n: options.top_n,
    });

    ProjectGraphReport {
        project_id,
        generated_at,
        summary,
        hotspots,
        unresolved_targets,
        external_targets,
        communities,
        bridge_summary,
        bridge_edges,
        degradation_details,
        suggested_investigation_questions,
        markdown,
    }
}

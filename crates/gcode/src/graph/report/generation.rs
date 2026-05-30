use std::collections::HashMap;

use gobby_core::degradation::ServiceState;

use crate::config::Context;

use super::RELATES_TO_CODE;
use super::loading::load_report_snapshot;
use super::render::{RenderMarkdownInput, render_markdown};
use super::summary::{
    normalize_bridge_edges, suggested_questions, summarize_bridge_edges, summarize_graph,
    summarize_hotspots, target_frequencies,
};
use super::time::now_iso8601;
use super::types::{
    BridgeEdgeInput, ProjectGraphReport, ProjectGraphReportError, ProjectGraphReportOptions,
    ReportDegradation, ReportGraphSnapshot,
};

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

    let connection_config = config.connection_config();
    let options = options.normalized();
    let result = gobby_core::falkor::with_graph(
        Some(&connection_config),
        &config.graph_name,
        ReportGraphSnapshot::default(),
        |client| load_report_snapshot(client, &ctx.project_id, options.top_n),
    );

    match result {
        Ok((snapshot, ServiceState::Available)) => Ok(generate_report_from_snapshot_with_options(
            &ctx.project_id,
            now_iso8601(),
            snapshot,
            options,
        )),
        Ok((_, ServiceState::NotConfigured)) => {
            Err(ProjectGraphReportError::GraphServiceNotConfigured)
        }
        Ok((_, ServiceState::Unreachable { message })) => {
            Err(ProjectGraphReportError::GraphServiceUnreachable { message })
        }
        Err(error) => Err(ProjectGraphReportError::GraphQueryFailed {
            message: error.to_string(),
        }),
    }
}

pub fn empty_report(project_id: impl Into<String>) -> ProjectGraphReport {
    generate_report_from_snapshot(project_id, now_iso8601(), ReportGraphSnapshot::default())
}

pub(super) fn generate_report_from_snapshot(
    project_id: impl Into<String>,
    generated_at: impl Into<String>,
    snapshot: ReportGraphSnapshot,
) -> ProjectGraphReport {
    generate_report_from_snapshot_with_options(
        project_id,
        generated_at,
        snapshot,
        ProjectGraphReportOptions::default().normalized(),
    )
}

fn generate_report_from_snapshot_with_options(
    project_id: impl Into<String>,
    generated_at: impl Into<String>,
    snapshot: ReportGraphSnapshot,
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
    let hotspots = snapshot.hotspots.clone().unwrap_or_else(|| {
        summarize_hotspots(&snapshot.nodes, &snapshot.code_edges, options.top_n)
    });
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

    let (bridge_edges, mut degradation_details) = match snapshot.bridge_edges {
        BridgeEdgeInput::Available(edges) => (normalize_bridge_edges(edges), vec![]),
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
        bridge_summary,
        bridge_edges,
        degradation_details,
        suggested_investigation_questions,
        markdown,
    }
}

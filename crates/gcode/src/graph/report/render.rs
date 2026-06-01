use std::collections::BTreeMap;

use super::types::{
    BridgeReportSummary, GraphHotspot, GraphReportHotspots, GraphReportSummary, ReportDegradation,
    TargetFrequency,
};

pub(super) struct RenderMarkdownInput<'a> {
    pub(super) project_id: &'a str,
    pub(super) generated_at: &'a str,
    pub(super) summary: &'a GraphReportSummary,
    pub(super) hotspots: &'a GraphReportHotspots,
    pub(super) unresolved_targets: &'a [TargetFrequency],
    pub(super) external_targets: &'a [TargetFrequency],
    pub(super) bridge_summary: Option<&'a BridgeReportSummary>,
    pub(super) degradation_details: &'a [ReportDegradation],
    pub(super) top_n: usize,
}

pub(super) fn render_markdown(input: RenderMarkdownInput<'_>) -> String {
    let mut lines = vec![
        "# Project Graph Report".to_string(),
        String::new(),
        format!("- Project: {}", inline_code(input.project_id)),
        format!("- Generated: {}", inline_code(input.generated_at)),
        format!("- Nodes: {}", input.summary.node_count),
        format!("- Edges: {}", input.summary.edge_count),
    ];

    if !input.summary.code_edge_counts.is_empty() {
        lines.push(format!(
            "- Code edges: {}",
            named_counts_inline(&input.summary.code_edge_counts)
        ));
    }

    append_hotspot_section(
        &mut lines,
        "High-degree files",
        &input.hotspots.high_degree_files,
        input.top_n,
    );
    append_hotspot_section(
        &mut lines,
        "High-degree symbols",
        &input.hotspots.high_degree_symbols,
        input.top_n,
    );
    append_hotspot_section(
        &mut lines,
        "Incoming-call hotspots",
        &input.hotspots.incoming_call_hotspots,
        input.top_n,
    );
    append_target_section(
        &mut lines,
        "Unresolved call targets",
        input.unresolved_targets,
        input.top_n,
    );
    append_target_section(
        &mut lines,
        "External call targets",
        input.external_targets,
        input.top_n,
    );

    if let Some(summary) = input.bridge_summary {
        lines.push(String::new());
        lines.push("## RELATES_TO_CODE bridges".to_string());
        lines.push(format!(
            "- {} inferred read-only edge(s)",
            summary.edge_count
        ));
        if let Some(range) = &summary.confidence_range {
            lines.push(format!("- Confidence: {:.3}..{:.3}", range.min, range.max));
        }
    }

    if !input.degradation_details.is_empty() {
        lines.push(String::new());
        lines.push("## Degradation".to_string());
        for detail in input.degradation_details {
            lines.push(format!(
                "- {}: {}",
                inline_code(&detail.input),
                markdown_text(&detail.detail)
            ));
        }
    }

    lines.join("\n")
}

fn append_hotspot_section(
    lines: &mut Vec<String>,
    title: &str,
    hotspots: &[GraphHotspot],
    top_n: usize,
) {
    if top_n == 0 || hotspots.is_empty() {
        return;
    }
    lines.push(String::new());
    lines.push(format!("## {title}"));
    for hotspot in hotspots.iter().take(top_n) {
        lines.push(format!(
            "- {} ({}, degree {})",
            inline_code(&hotspot.name),
            hotspot.node_type,
            hotspot.degree
        ));
    }
}

fn append_target_section(
    lines: &mut Vec<String>,
    title: &str,
    targets: &[TargetFrequency],
    top_n: usize,
) {
    if top_n == 0 || targets.is_empty() {
        return;
    }
    lines.push(String::new());
    lines.push(format!("## {title}"));
    for target in targets.iter().take(top_n) {
        lines.push(format!(
            "- {} ({})",
            inline_code(&target.name),
            target.count
        ));
    }
}

fn inline_code(value: &str) -> String {
    let delimiter = "`".repeat(max_backtick_run(value).saturating_add(1).max(1));
    if value.starts_with('`') || value.ends_with('`') {
        format!("{delimiter} {value} {delimiter}")
    } else {
        format!("{delimiter}{value}{delimiter}")
    }
}

fn max_backtick_run(value: &str) -> usize {
    let mut max_run = 0usize;
    let mut current_run = 0usize;
    for ch in value.chars() {
        if ch == '`' {
            current_run += 1;
            max_run = max_run.max(current_run);
        } else {
            current_run = 0;
        }
    }
    max_run
}

fn markdown_text(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('`', "\\`")
        .replace('*', "\\*")
        .replace('_', "\\_")
        .replace('[', "\\[")
        .replace(']', "\\]")
        .replace('<', "\\<")
        .replace('>', "\\>")
        .replace('\n', " ")
}

fn named_counts_inline(counts: &BTreeMap<String, usize>) -> String {
    counts
        .iter()
        .map(|(name, count)| format!("{name}={count}"))
        .collect::<Vec<_>>()
        .join(", ")
}

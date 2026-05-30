use crate::config::Context;
use crate::graph::code_graph::{self, GraphBlastRadiusTarget, GraphPayload};
use crate::graph::report::{ProjectGraphReport, ProjectGraphReportOptions};
use crate::output::{self, Format};

fn format_graph_payload_text(payload: &GraphPayload) -> String {
    let mut lines = Vec::new();
    lines.push(format!(
        "nodes: {}, links: {}",
        payload.nodes.len(),
        payload.links.len()
    ));
    if let Some(center) = &payload.center {
        lines.push(format!("center: {center}"));
    }
    for node in &payload.nodes {
        let file = node.file_path.as_deref().unwrap_or("");
        if file.is_empty() {
            lines.push(format!(
                "node {} [{}] {}",
                node.id, node.node_type, node.name
            ));
        } else {
            lines.push(format!(
                "node {} [{}] {} {}",
                node.id, node.node_type, node.name, file
            ));
        }
    }
    for link in &payload.links {
        lines.push(format!(
            "link {} -[{}]-> {}",
            link.source, link.link_type, link.target
        ));
    }
    lines.join("\n")
}

fn print_graph_payload(payload: &GraphPayload, format: Format) -> anyhow::Result<()> {
    match format {
        Format::Json => output::print_json(payload),
        Format::Text => output::print_text(&format_graph_payload_text(payload)),
    }
}

pub(super) fn format_report_text(report: &ProjectGraphReport) -> String {
    report.markdown.clone()
}

pub fn report(ctx: &Context, top_n: usize, format: Format) -> anyhow::Result<()> {
    let report = crate::graph::report::generate_report_with_options(
        ctx,
        ProjectGraphReportOptions { top_n },
    )?;
    match format {
        Format::Json => output::print_json(&report),
        Format::Text => output::print_text(&format_report_text(&report)),
    }
}

pub fn overview(ctx: &Context, limit: usize, format: Format) -> anyhow::Result<()> {
    let payload = code_graph::project_overview_graph(ctx, limit)?;
    print_graph_payload(&payload, format)
}

pub fn file(ctx: &Context, file_path: &str, format: Format) -> anyhow::Result<()> {
    let payload = code_graph::file_graph(ctx, file_path)?;
    print_graph_payload(&payload, format)
}

pub fn neighbors(
    ctx: &Context,
    symbol_id: &str,
    limit: usize,
    format: Format,
) -> anyhow::Result<()> {
    let payload = code_graph::symbol_neighbors(ctx, symbol_id, limit)?;
    print_graph_payload(&payload, format)
}

pub fn graph_blast_radius(
    ctx: &Context,
    symbol_id: Option<&str>,
    file_path: Option<&str>,
    depth: usize,
    limit: usize,
    format: Format,
) -> anyhow::Result<()> {
    let target = match (symbol_id, file_path) {
        (Some(symbol_id), None) => GraphBlastRadiusTarget::SymbolId(symbol_id.to_string()),
        (None, Some(file_path)) => GraphBlastRadiusTarget::FilePath(file_path.to_string()),
        _ => anyhow::bail!("provide exactly one of --symbol-id or --file"),
    };
    let payload = code_graph::blast_radius_graph(ctx, target, depth, limit)?;
    print_graph_payload(&payload, format)
}

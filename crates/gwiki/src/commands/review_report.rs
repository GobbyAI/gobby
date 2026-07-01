use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::path::{Path, PathBuf};

use gobby_core::ai_context::AiConfigSource;
use gobby_core::config::FalkorConfig;
use gobby_core::degradation::DegradationKind;
use gobby_core::gobby_home;
use gobby_core::graph_analytics::{
    self, AnalyticsEdge, AnalyticsGraph, AnalyticsNode, CentralityScore, NodeRef,
};
use serde_json::json;

use crate::code_graph::{AffectedPage, CodeChangeSet, CodeGraphEdge, CodeGraphQuery};
use crate::provenance::ProvenanceGraph;
use crate::search::SearchScope;
use crate::support::env::database_url_for;
use crate::support::scope::resolve_selection_context;
use crate::support::search::PostgresConfigSource;
use crate::support::text::sanitize_code_path;
use crate::{
    CommandOutcome, ReviewReportOptions, ScopeIdentity, ScopeSelection, WikiError, exports,
};

const DEGRADED_FALKORDB_UNAVAILABLE: &str = "falkordb_unavailable";
const DEGRADED_GCODE_CODE_GRAPH_UNAVAILABLE: &str = "gcode_code_graph_unavailable";
const DEGRADED_SHARED_CODE_GRAPH_UNAVAILABLE: &str = "shared_code_graph_unavailable";

pub(crate) fn execute(
    selection: ScopeSelection,
    options: ReviewReportOptions,
) -> Result<CommandOutcome, WikiError> {
    let resolved = resolve_selection_context(&selection)?;
    let changes = ChangeSetInput::from_options(options)?;
    let database_url =
        database_url_for("gwiki review-report")?.ok_or_else(|| WikiError::Config {
            detail: "gwiki review-report requires PostgreSQL index configuration".to_string(),
        })?;
    let mut conn = gobby_core::postgres::connect_readonly(&database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for gwiki review-report: {error}"),
        }
    })?;
    let falkor = optional_falkor_config(&mut conn)?;
    let facts = crate::falkor_graph::load_wiki_graph_facts(&mut conn, &resolved.search_scope)?;
    let provenance = ProvenanceGraph::load_from_vault(resolved.scope.root())?;
    let affected = crate::code_graph::affected_pages_for_changes(
        falkor.as_ref(),
        resolved.search_scope.scope_value(),
        &provenance,
        changes.as_code_change_set(),
    )
    .map_err(|error| WikiError::Config {
        detail: format!("query review-report affected pages: {error}"),
    })?;
    let mut degraded_sources = affected
        .degradations
        .iter()
        .map(degradation_source)
        .collect::<Vec<_>>();
    let neighborhoods = graph_neighborhoods(
        falkor.as_ref(),
        &resolved.search_scope,
        &changes,
        &mut degraded_sources,
    )?;
    let health = crate::health::inspect(resolved.scope.root(), resolved.output_scope.clone())?;
    let graph_ready = falkor.is_some()
        && matches!(resolved.search_scope, SearchScope::Project { .. })
        && !degraded_sources
            .iter()
            .any(|source| is_graph_blocking_degraded_source(source));
    let analytics_graph = graph_ready.then(|| analytics_graph_from_edges(&changes, &neighborhoods));
    let report = build_report_from_parts(ReportParts {
        scope: resolved.output_scope.clone(),
        changes: changes.clone(),
        affected_pages: affected.pages,
        stale_pages: health.stale_pages,
        neighborhoods,
        analytics_graph,
        degraded_sources,
    });
    let markdown = render_markdown(&report);
    let artifact =
        exports::export_markdown_report(resolved.scope.root(), changes.output.clone(), markdown)?;
    let payload = json!({
        "command": "review-report",
        "scope": resolved.output_scope,
        "artifact": artifact,
        "degraded": report.degraded,
        "degraded_sources": report.degraded_sources,
        "documents_indexed": facts.documents.len(),
    });
    let text = format!(
        "Exported review report\nScope: {}\nArtifact: {}\nDegraded: {}",
        report.scope,
        artifact.path.display(),
        report.degraded
    );
    Ok(super::scoped_outcome(
        "review-report",
        &report.scope,
        payload,
        text,
    ))
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ChangeSetInput {
    files: Vec<String>,
    symbols: Vec<String>,
    diff_path: Option<PathBuf>,
    output: String,
}

impl ChangeSetInput {
    fn from_options(options: ReviewReportOptions) -> Result<Self, WikiError> {
        let mut files = options.files;
        if let Some(path) = &options.diff_path {
            files.extend(changed_files_from_diff(path)?);
        }
        files = unique_non_empty(files);
        let symbols = unique_non_empty(options.symbols);
        if files.is_empty() && symbols.is_empty() {
            return Err(WikiError::InvalidInput {
                field: "review-report",
                message: "pass at least one --file, --symbol, or --diff change input".to_string(),
            });
        }
        Ok(Self {
            files,
            symbols,
            diff_path: options.diff_path,
            output: options.output,
        })
    }

    fn as_code_change_set(&self) -> CodeChangeSet {
        CodeChangeSet {
            files: self.files.clone(),
            symbols: self.symbols.clone(),
        }
    }
}

#[derive(Debug, Clone)]
struct ReportParts {
    scope: ScopeIdentity,
    changes: ChangeSetInput,
    affected_pages: Vec<AffectedPage>,
    stale_pages: Vec<PathBuf>,
    neighborhoods: Vec<CodeGraphEdge>,
    analytics_graph: Option<AnalyticsGraph>,
    degraded_sources: Vec<String>,
}

#[derive(Debug, Clone, PartialEq)]
struct ReviewReport {
    command: &'static str,
    scope: ScopeIdentity,
    degraded: bool,
    degraded_sources: Vec<String>,
    changes: ChangeSetInput,
    affected_pages: Vec<ReviewAffectedPage>,
    stale_docs: Vec<String>,
    changed_graph_neighborhoods: Vec<CodeGraphEdge>,
    risky_dependency_shifts: Vec<RiskyDependencyShift>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ReviewAffectedPage {
    page_path: String,
    source_ids: Vec<String>,
    source_paths: Vec<String>,
}

#[derive(Debug, Clone, PartialEq)]
struct RiskyDependencyShift {
    node: NodeRef,
    degree: usize,
    score: f64,
    bridge: bool,
    reasons: Vec<String>,
}

fn build_report_from_parts(parts: ReportParts) -> ReviewReport {
    let degraded_sources = unique_non_empty(parts.degraded_sources);
    let risky_dependency_shifts = parts
        .analytics_graph
        .as_ref()
        .map(|graph| risky_dependency_shifts(graph, &parts.changes))
        .unwrap_or_default();
    ReviewReport {
        command: "review-report",
        scope: parts.scope,
        degraded: !degraded_sources.is_empty(),
        degraded_sources,
        changes: parts.changes,
        affected_pages: parts
            .affected_pages
            .into_iter()
            .map(review_affected_page)
            .collect(),
        stale_docs: parts
            .stale_pages
            .into_iter()
            .map(|path| path.display().to_string())
            .collect(),
        changed_graph_neighborhoods: parts.neighborhoods,
        risky_dependency_shifts,
    }
}

fn render_markdown(report: &ReviewReport) -> String {
    let mut markdown = String::new();
    markdown.push_str("---\n");
    markdown.push_str("command: review-report\n");
    markdown.push_str(&format!("scope: {}\n", report.scope));
    markdown.push_str(&format!("degraded: {}\n", report.degraded));
    markdown.push_str("degraded_sources: [");
    markdown.push_str(&report.degraded_sources.join(", "));
    markdown.push_str("]\n---\n\n");
    markdown.push_str("# Review report\n\n");
    render_changes(&mut markdown, &report.changes);
    render_affected_pages(&mut markdown, &report.affected_pages);
    render_stale_docs(&mut markdown, &report.stale_docs);
    render_neighborhoods(&mut markdown, &report.changed_graph_neighborhoods);
    render_risky_shifts(&mut markdown, report);
    markdown
}

fn render_changes(markdown: &mut String, changes: &ChangeSetInput) {
    markdown.push_str("## Change set\n\n");
    render_string_list(markdown, "Files", &changes.files);
    render_string_list(markdown, "Symbols", &changes.symbols);
    if let Some(path) = &changes.diff_path {
        markdown.push_str("- Diff: `");
        markdown.push_str(&path.display().to_string());
        markdown.push_str("`\n");
    }
    markdown.push('\n');
}

fn render_affected_pages(markdown: &mut String, pages: &[ReviewAffectedPage]) {
    markdown.push_str("## Affected wiki pages\n\n");
    if pages.is_empty() {
        markdown.push_str("- none\n\n");
        return;
    }
    for page in pages {
        markdown.push_str("- `");
        markdown.push_str(&page.page_path);
        markdown.push('`');
        if !page.source_paths.is_empty() {
            markdown.push_str(" from ");
            markdown.push_str(&page.source_paths.join(", "));
        }
        markdown.push('\n');
    }
    markdown.push('\n');
}

fn render_stale_docs(markdown: &mut String, stale_docs: &[String]) {
    markdown.push_str("## Stale docs\n\n");
    render_string_list(markdown, "Docs", stale_docs);
    markdown.push('\n');
}

fn render_neighborhoods(markdown: &mut String, neighborhoods: &[CodeGraphEdge]) {
    markdown.push_str("## Changed graph neighborhoods\n\n");
    if neighborhoods.is_empty() {
        markdown.push_str("- none\n\n");
        return;
    }
    for edge in neighborhoods {
        markdown.push_str("- ");
        markdown.push_str(&edge.edge);
        markdown.push_str(": `");
        markdown.push_str(&edge.source);
        markdown.push_str("` -> `");
        markdown.push_str(&edge.target);
        markdown.push('`');
        if let Some(path) = &edge.file_path {
            markdown.push_str(" (");
            markdown.push_str(path);
            if let Some(line) = edge.line {
                markdown.push(':');
                markdown.push_str(&line.to_string());
            }
            markdown.push(')');
        }
        markdown.push('\n');
    }
    markdown.push('\n');
}

fn render_risky_shifts(markdown: &mut String, report: &ReviewReport) {
    markdown.push_str("## Risky dependency shifts\n\n");
    if report
        .degraded_sources
        .iter()
        .any(|source| is_graph_blocking_degraded_source(source))
    {
        markdown.push_str("- graph/analytics unavailable; risky dependency shifts omitted\n");
        return;
    }
    if report.risky_dependency_shifts.is_empty() {
        markdown.push_str("- none\n");
        return;
    }
    for risk in &report.risky_dependency_shifts {
        markdown.push_str("- `");
        markdown.push_str(&risk.node.id);
        markdown.push_str("` degree ");
        markdown.push_str(&risk.degree.to_string());
        markdown.push_str(", score ");
        markdown.push_str(&format!("{:.3}", risk.score));
        markdown.push_str(" (");
        markdown.push_str(&risk.reasons.join(", "));
        markdown.push_str(")\n");
    }
}

fn graph_neighborhoods(
    falkor: Option<&FalkorConfig>,
    scope: &SearchScope,
    changes: &ChangeSetInput,
    degraded_sources: &mut Vec<String>,
) -> Result<Vec<CodeGraphEdge>, WikiError> {
    let SearchScope::Project { project_id } = scope else {
        degraded_sources.push(DEGRADED_SHARED_CODE_GRAPH_UNAVAILABLE.to_string());
        return Ok(Vec::new());
    };
    let Some(falkor) = falkor else {
        degraded_sources.push(DEGRADED_FALKORDB_UNAVAILABLE.to_string());
        degraded_sources.push(DEGRADED_SHARED_CODE_GRAPH_UNAVAILABLE.to_string());
        return Ok(Vec::new());
    };
    let mut edges = Vec::new();
    for file in &changes.files {
        let (mut file_edges, degradation) =
            crate::code_graph::code_edges(Some(falkor), project_id, CodeGraphQuery::File(file))
                .map_err(|error| WikiError::Config {
                    detail: format!("query review-report file graph neighborhood: {error}"),
                })?;
        edges.append(&mut file_edges);
        if let Some(degradation) = degradation {
            degraded_sources.push(degradation_source(&degradation));
        }
    }
    for symbol in &changes.symbols {
        let (mut symbol_edges, degradation) =
            crate::code_graph::code_edges(Some(falkor), project_id, CodeGraphQuery::Symbol(symbol))
                .map_err(|error| WikiError::Config {
                    detail: format!("query review-report symbol graph neighborhood: {error}"),
                })?;
        edges.append(&mut symbol_edges);
        if let Some(degradation) = degradation {
            degraded_sources.push(degradation_source(&degradation));
        }
    }
    Ok(unique_edges(edges))
}

fn analytics_graph_from_edges(changes: &ChangeSetInput, edges: &[CodeGraphEdge]) -> AnalyticsGraph {
    let changed = changed_node_ids(changes, edges);
    let mut nodes = BTreeMap::<String, AnalyticsNode>::new();
    for id in &changed {
        nodes.insert(
            id.clone(),
            AnalyticsNode {
                id: id.clone(),
                kind: "changed".to_string(),
                weight: 3.0,
            },
        );
    }
    let graph_edges = edges
        .iter()
        .map(|edge| {
            for id in [&edge.source, &edge.target] {
                nodes.entry(id.clone()).or_insert_with(|| AnalyticsNode {
                    id: id.clone(),
                    kind: "neighbor".to_string(),
                    weight: 1.0,
                });
            }
            AnalyticsEdge {
                source: edge.source.clone(),
                target: edge.target.clone(),
                kind: edge.edge.clone(),
                weight: graph_analytics::weight_for_kind(&edge.edge),
            }
        })
        .collect();
    AnalyticsGraph {
        nodes: nodes.into_values().collect(),
        edges: graph_edges,
    }
}

fn risky_dependency_shifts(
    graph: &AnalyticsGraph,
    changes: &ChangeSetInput,
) -> Vec<RiskyDependencyShift> {
    let analytics = graph_analytics::analyze(graph);
    let changed = changed_node_ids_from_graph(changes, graph);
    let bridges = analytics
        .bridges
        .iter()
        .map(|node| node.id.as_str())
        .collect::<HashSet<_>>();
    let max_score = analytics
        .centrality
        .iter()
        .map(|score| score.score)
        .fold(0.0_f64, f64::max);
    let mut risks = analytics
        .centrality
        .into_iter()
        .filter(|score| changed.contains(&score.node.id))
        .filter_map(|score| risk_from_score(score, &bridges, max_score))
        .collect::<Vec<_>>();
    risks.sort_by(|left, right| {
        right
            .score
            .total_cmp(&left.score)
            .then_with(|| left.node.id.cmp(&right.node.id))
    });
    risks
}

fn risk_from_score(
    score: CentralityScore,
    bridges: &HashSet<&str>,
    max_score: f64,
) -> Option<RiskyDependencyShift> {
    let bridge = bridges.contains(score.node.id.as_str());
    let high_centrality = score.degree >= 2 || (max_score > 0.0 && score.score == max_score);
    if !bridge && !high_centrality {
        return None;
    }
    let mut reasons = Vec::new();
    if high_centrality {
        reasons.push("high-centrality node touched".to_string());
    }
    if bridge {
        reasons.push("bridge node touched".to_string());
    }
    Some(RiskyDependencyShift {
        node: score.node,
        degree: score.degree,
        score: score.score,
        bridge,
        reasons,
    })
}

fn changed_node_ids(changes: &ChangeSetInput, edges: &[CodeGraphEdge]) -> BTreeSet<String> {
    let mut changed = changes.symbols.iter().cloned().collect::<BTreeSet<_>>();
    for edge in edges {
        if edge
            .file_path
            .as_deref()
            .is_some_and(|path| changes.files.iter().any(|file| file == path))
        {
            changed.insert(edge.source.clone());
            changed.insert(edge.target.clone());
        }
    }
    changed
}

fn changed_node_ids_from_graph(
    changes: &ChangeSetInput,
    graph: &AnalyticsGraph,
) -> BTreeSet<String> {
    let mut changed = changes.symbols.iter().cloned().collect::<BTreeSet<_>>();
    for node in &graph.nodes {
        if node.kind == "changed" {
            changed.insert(node.id.clone());
        }
    }
    changed
}

fn changed_files_from_diff(path: &Path) -> Result<Vec<String>, WikiError> {
    let contents = std::fs::read_to_string(path).map_err(|error| WikiError::Io {
        action: "read review-report diff",
        path: Some(path.to_path_buf()),
        source: error,
    })?;
    Ok(parse_unified_diff_files(&contents))
}

fn parse_unified_diff_files(contents: &str) -> Vec<String> {
    let mut files = BTreeSet::new();
    // Diff headers are attacker-influenced input (review diffs can come from
    // arbitrary branches), so every candidate path is validated through
    // sanitize_code_path before it can scope downstream graph queries:
    // absolute paths, parent-directory traversal, and empty paths are dropped.
    let mut insert_sanitized = |candidate: &str| {
        if let Some(path) = sanitize_code_path(candidate) {
            files.insert(path);
        }
    };
    for line in contents.lines() {
        if let Some(path) = line
            .strip_prefix("+++ b/")
            .or_else(|| line.strip_prefix("--- a/"))
        {
            // `+++ b/` is the new path; `--- a/` keeps deleted files visible when
            // the right side is `/dev/null`.
            if path != "/dev/null" {
                insert_sanitized(path);
            }
        } else if let Some(rest) = line.strip_prefix("diff --git a/")
            && let Some((left, right)) = rest.split_once(" b/")
        {
            if right.trim().is_empty() {
                // Malformed or truncated diff headers have no right side; keep the
                // left path so the report still scopes the affected deleted file.
                insert_sanitized(trim_diff_path(left));
            } else {
                // Rename headers carry old and new paths; review impact follows the new path.
                insert_sanitized(trim_diff_path(right));
            }
        }
    }
    files.into_iter().collect()
}

fn trim_diff_path(path: &str) -> &str {
    path.trim().trim_matches('"')
}

fn review_affected_page(page: AffectedPage) -> ReviewAffectedPage {
    ReviewAffectedPage {
        page_path: page.page_path.display().to_string(),
        source_ids: page.source_ids,
        source_paths: page
            .source_paths
            .into_iter()
            .map(|path| path.display().to_string())
            .collect(),
    }
}

fn render_string_list(markdown: &mut String, label: &str, values: &[String]) {
    if values.is_empty() {
        markdown.push_str("- ");
        markdown.push_str(label);
        markdown.push_str(": none\n");
        return;
    }
    for value in values {
        markdown.push_str("- ");
        markdown.push_str(label);
        markdown.push_str(": `");
        markdown.push_str(value);
        markdown.push_str("`\n");
    }
}

fn unique_non_empty(values: Vec<String>) -> Vec<String> {
    values
        .into_iter()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

fn unique_edges(edges: Vec<CodeGraphEdge>) -> Vec<CodeGraphEdge> {
    let mut seen = BTreeSet::new();
    edges
        .into_iter()
        .filter(|edge| {
            seen.insert((
                edge.edge.clone(),
                edge.source.clone(),
                edge.target.clone(),
                edge.file_path.clone(),
                edge.line,
            ))
        })
        .collect()
}

fn degradation_source(degradation: &DegradationKind) -> String {
    match degradation {
        DegradationKind::ServiceUnavailable { service, .. } if service == "gcode_code_graph" => {
            DEGRADED_GCODE_CODE_GRAPH_UNAVAILABLE.to_string()
        }
        DegradationKind::ServiceUnavailable { service, .. } => format!("{service}_unavailable"),
        DegradationKind::PartialSearch { unavailable, .. } => {
            format!("partial_search_missing_{}", unavailable.join("_"))
        }
        DegradationKind::PartialData { component, .. } => format!("{component}_partial"),
        DegradationKind::StaleIndex { .. } => "stale_index".to_string(),
        DegradationKind::SkippedArtifacts { .. } => "skipped_artifacts".to_string(),
    }
}

fn is_graph_blocking_degraded_source(source: &str) -> bool {
    matches!(
        source,
        DEGRADED_FALKORDB_UNAVAILABLE
            | DEGRADED_GCODE_CODE_GRAPH_UNAVAILABLE
            | DEGRADED_SHARED_CODE_GRAPH_UNAVAILABLE
    )
}

fn optional_falkor_config(conn: &mut postgres::Client) -> Result<Option<FalkorConfig>, WikiError> {
    let gobby_home = gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki review-report: {error}"),
    })?;
    let primary = PostgresConfigSource { conn };
    let mut source =
        AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home).map_err(|error| {
            WikiError::Config {
                detail: format!("failed to resolve optional review-report config: {error}"),
            }
        })?;
    Ok(gobby_core::config::resolve_falkordb_config(&mut source))
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use gobby_core::graph_analytics::{
        AnalyticsEdge, AnalyticsGraph, AnalyticsNode, weight_for_kind,
    };

    use crate::ScopeIdentity;
    use crate::code_graph::{AffectedPage, CodeGraphEdge};

    use super::*;

    #[test]
    fn review_report_renders_markdown_with_risks_and_metadata() {
        let report = build_report_from_parts(ReportParts {
            scope: ScopeIdentity::project("project-1"),
            changes: ChangeSetInput {
                files: vec!["src/lib.rs".to_string()],
                symbols: vec!["symbol-a".to_string()],
                diff_path: None,
                output: "review-report.md".to_string(),
            },
            affected_pages: vec![AffectedPage {
                page_path: PathBuf::from("code/lib.md"),
                source_ids: vec!["src-lib".to_string()],
                source_paths: vec![PathBuf::from("src/lib.rs")],
            }],
            stale_pages: vec![PathBuf::from("code/lib.md")],
            neighborhoods: vec![CodeGraphEdge {
                edge: "CALLS".to_string(),
                source: "symbol-a".to_string(),
                target: "symbol-b".to_string(),
                detail: Some("b".to_string()),
                file_path: Some("src/lib.rs".to_string()),
                line: Some(12),
            }],
            analytics_graph: Some(AnalyticsGraph {
                nodes: vec![
                    AnalyticsNode {
                        id: "symbol-a".to_string(),
                        kind: "changed".to_string(),
                        weight: 3.0,
                    },
                    AnalyticsNode {
                        id: "symbol-b".to_string(),
                        kind: "neighbor".to_string(),
                        weight: 1.0,
                    },
                    AnalyticsNode {
                        id: "symbol-c".to_string(),
                        kind: "neighbor".to_string(),
                        weight: 1.0,
                    },
                ],
                edges: vec![
                    AnalyticsEdge {
                        source: "symbol-a".to_string(),
                        target: "symbol-b".to_string(),
                        kind: "CALLS".to_string(),
                        weight: weight_for_kind("CALLS"),
                    },
                    AnalyticsEdge {
                        source: "symbol-a".to_string(),
                        target: "symbol-c".to_string(),
                        kind: "CALLS".to_string(),
                        weight: weight_for_kind("CALLS"),
                    },
                ],
            }),
            degraded_sources: Vec::new(),
        });

        assert!(!report.degraded);
        assert_eq!(report.risky_dependency_shifts.len(), 1);
        assert_eq!(report.risky_dependency_shifts[0].node.id, "symbol-a");

        let markdown = render_markdown(&report);
        assert!(markdown.contains("degraded: false"));
        assert!(markdown.contains("## Risky dependency shifts"));
        assert!(markdown.contains("symbol-a"));
        assert!(markdown.contains("## Affected wiki pages"));
        assert!(markdown.contains("code/lib.md"));
    }

    #[test]
    fn review_report_degrades_without_graph_analytics() {
        let report = build_report_from_parts(ReportParts {
            scope: ScopeIdentity::project("project-1"),
            changes: ChangeSetInput {
                files: vec!["src/lib.rs".to_string()],
                symbols: Vec::new(),
                diff_path: None,
                output: "review-report.md".to_string(),
            },
            affected_pages: Vec::new(),
            stale_pages: Vec::new(),
            neighborhoods: Vec::new(),
            analytics_graph: None,
            degraded_sources: vec!["shared_code_graph_unavailable".to_string()],
        });

        assert!(report.degraded);
        assert!(report.risky_dependency_shifts.is_empty());
        let markdown = render_markdown(&report);
        assert!(markdown.contains("degraded: true"));
        assert!(markdown.contains("degraded_sources: [shared_code_graph_unavailable]"));
        assert!(markdown.contains("graph/analytics unavailable"));
    }

    #[test]
    fn review_report_maps_semantic_partial_data_degradation() {
        let source = degradation_source(&DegradationKind::PartialData {
            component: "semantic".to_string(),
            message: "global scope: semantic fan-out not implemented".to_string(),
        });

        assert_eq!(source, "semantic_partial");
    }

    #[test]
    fn parse_unified_diff_files_uses_new_rename_path_and_skips_empty_right_path() {
        let files = parse_unified_diff_files(
            "diff --git a/src/old.rs b/src/new.rs\n\
             similarity index 99%\n\
             diff --git a/src/deleted.rs b/\n",
        );

        assert_eq!(
            files,
            vec!["src/deleted.rs".to_string(), "src/new.rs".to_string()]
        );
    }

    #[test]
    fn parse_unified_diff_files_sanitizes_unsafe_paths() {
        let files = parse_unified_diff_files(
            "diff --git a/src/./safe.rs b/src/./safe.rs\n\
             +++ b/../escape.rs\n\
             diff --git a/src/old.rs b//tmp/absolute.rs\n\
             --- a/src/./deleted.rs\n\
             +++ /dev/null\n",
        );

        assert_eq!(
            files,
            vec!["src/deleted.rs".to_string(), "src/safe.rs".to_string()]
        );
    }
}

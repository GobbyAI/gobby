use std::collections::BTreeSet;
use std::path::Path;

use gobby_core::ai_context::AiConfigSource;
use gobby_core::config::{FalkorConfig, resolve_falkordb_config};
use gobby_core::gobby_home;

use crate::commands::ask::dedup::{AskOutputDedup, mark_degraded_source};
use crate::graph::WikiGraphFacts;
use crate::graph::context::{GraphContextCodeEdge, GraphContextOptions, build_context_pack};
use crate::output::{AskCodeCitationOutput, AskCodeEdgeOutput, AskOutput, AskRelatedPageOutput};
use crate::search::SearchScope;
use crate::support::config::shared_code_graph_limits_from_conn;
use crate::support::env::database_url_for;
use crate::support::scope::resolve_selection_context;
use crate::support::search::PostgresConfigSource;
use crate::{ScopeSelection, WikiError};

pub(super) fn enrich_with_attached_unified_graph_context(
    output: &mut AskOutput,
    selection: &ScopeSelection,
) -> Result<(), WikiError> {
    let Some(database_url) = database_url_for("gwiki ask")? else {
        let mut dedup = AskOutputDedup::from_output(output);
        mark_degraded_source(output, &mut dedup, "shared_code_graph_unavailable");
        return Ok(());
    };
    let resolved = resolve_selection_context(selection)?;
    let mut conn = gobby_core::postgres::connect_readonly(&database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for gwiki ask graph context: {error}"),
        }
    })?;
    let mut facts = crate::falkor_graph::load_wiki_graph_facts(&mut conn, &resolved.search_scope)?;
    let mut degraded_sources = Vec::new();
    let mut truncated_components = Vec::new();
    let limits = shared_code_graph_limits_from_conn(&mut conn)?;
    let falkor = optional_falkor_config(&mut conn)?;
    match (falkor, &resolved.search_scope) {
        (Some(falkor), SearchScope::Project { project_id }) => {
            match crate::falkor_graph::load_code_graph_edges(
                &falkor,
                project_id,
                &facts.documents,
                limits,
            ) {
                Ok(code_graph) => {
                    if code_graph.truncation.is_truncated() {
                        degraded_sources.push(
                            crate::falkor_graph::SHARED_CODE_GRAPH_TRUNCATED_SOURCE.to_string(),
                        );
                        truncated_components.extend(code_graph.truncation.components);
                    }
                    facts.code_edges = code_graph.edges;
                }
                Err(error) => {
                    log::warn!("failed to load shared code graph for gwiki ask: {error}");
                    degraded_sources.push("shared_code_graph_unavailable".to_string());
                }
            }
        }
        (Some(_), SearchScope::Global | SearchScope::Topic { .. }) => {
            degraded_sources.push("shared_code_graph_unavailable".to_string());
        }
        (None, _) => {
            degraded_sources.push("falkordb_unavailable".to_string());
            degraded_sources.push("shared_code_graph_unavailable".to_string());
        }
    }
    enrich_with_unified_graph_context(output, &facts, degraded_sources, truncated_components);
    Ok(())
}

fn optional_falkor_config(conn: &mut postgres::Client) -> Result<Option<FalkorConfig>, WikiError> {
    let gobby_home = gobby_home().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve Gobby home for gwiki ask: {error}"),
    })?;
    let primary = PostgresConfigSource { conn };
    let mut source =
        AiConfigSource::with_primary_from_gobby_home(primary, &gobby_home).map_err(|error| {
            WikiError::Config {
                detail: format!("failed to resolve optional ask graph config: {error}"),
            }
        })?;

    Ok(resolve_falkordb_config(&mut source))
}

fn enrich_with_unified_graph_context(
    output: &mut AskOutput,
    facts: &WikiGraphFacts,
    degraded_sources: Vec<String>,
    truncated_components: Vec<String>,
) {
    let options = if degraded_sources.is_empty() {
        GraphContextOptions::available()
    } else {
        GraphContextOptions::degraded(degraded_sources)
    }
    .with_truncated_components(truncated_components);
    let pack = build_context_pack(facts, options);
    let mut dedup = AskOutputDedup::from_output(output);
    for source in &pack.degradation.degraded_sources {
        mark_degraded_source(output, &mut dedup, source);
    }
    if pack.degradation.truncated {
        output.truncated = true;
        for component in &pack.degradation.truncated_components {
            dedup.push_truncated_component(output, component.clone());
        }
    }

    let mut related_paths = output
        .related_pages
        .iter()
        .map(|page| page.path.clone())
        .collect::<BTreeSet<_>>();
    for neighborhood in &pack.neighborhoods {
        let path = Path::new(&neighborhood.path).to_path_buf();
        if related_paths.insert(path.clone()) {
            output.related_pages.push(AskRelatedPageOutput {
                title: neighborhood.title.clone(),
                path,
                score: 0.0,
            });
        }
        for citation in &neighborhood.citations {
            dedup.push_source(output, citation.clone());
        }
        for edge in neighborhood.calls.iter().chain(neighborhood.imports.iter()) {
            dedup.push_code_edge(output, code_edge_from_context(edge));
        }
        for citation in code_citations_from_context_edges(
            neighborhood.calls.iter().chain(neighborhood.imports.iter()),
        ) {
            dedup.push_code_citation(output, citation);
        }
    }
}

fn code_citations_from_context_edges<'a>(
    edges: impl Iterator<Item = &'a crate::graph::context::GraphContextCodeEdge>,
) -> Vec<AskCodeCitationOutput> {
    let mut citations = Vec::new();
    for edge in edges {
        if let Some(citation) = code_citation_from_endpoint(&edge.source, edge.line) {
            citations.push(citation);
        }
        if let Some(citation) = code_citation_from_endpoint(&edge.target, None) {
            citations.push(citation);
        }
    }
    citations
}

fn code_edge_from_context(edge: &GraphContextCodeEdge) -> AskCodeEdgeOutput {
    AskCodeEdgeOutput {
        source: edge.source.clone(),
        target: edge.target.clone(),
        kind: edge.kind.clone(),
        direction: edge.direction.clone(),
        line: edge.line,
        provenance: edge.provenance.clone(),
    }
}

fn code_citation_from_endpoint(
    endpoint: &str,
    line: Option<usize>,
) -> Option<AskCodeCitationOutput> {
    let (file, symbol) = match endpoint
        .split_once('#')
        .or_else(|| endpoint.rsplit_once(':'))
    {
        Some((file, symbol)) => (
            file.to_string(),
            (!symbol.is_empty()).then(|| symbol.to_string()),
        ),
        None if endpoint.contains('/') || endpoint.contains('.') => (endpoint.to_string(), None),
        None => return None,
    };
    if file.is_empty() {
        return None;
    }
    Some(AskCodeCitationOutput { file, line, symbol })
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use crate::ScopeIdentity;
    use crate::commands::ask::synthesis::synthesis_prompt;
    use crate::graph::{
        WikiGraphCodeEdge, WikiGraphDocument, WikiGraphFacts, WikiGraphLink, WikiGraphLinkTarget,
        WikiGraphSource,
    };
    use crate::output::{SearchOutput, SearchResultOutput, SearchResultType};

    use super::*;
    use crate::commands::ask::assembly::ask_output_from_search;

    #[test]
    fn ask_unified_graph_output_carries_code_citations_and_degradation() {
        let output = ask_output_from_search(SearchOutput::new(
            ScopeIdentity::project("project-1"),
            "Where is request handling wired?",
            10,
            vec![
                SearchResultOutput {
                    title: Some("Request handler".to_string()),
                    fusion_key: "project:project-1:code/files/src/handler.rs.md".to_string(),
                    wiki_page: PathBuf::from("code/files/src/handler.rs.md"),
                    source_path: PathBuf::from("src/handler.rs"),
                    result_type: SearchResultType::Code,
                    snippet: "fn handle() calls route().".to_string(),
                    score: 0.95,
                    sources: vec!["bm25".to_string(), "graph".to_string()],
                    explanations: Vec::new(),
                },
                SearchResultOutput {
                    title: Some("Architecture".to_string()),
                    fusion_key: "project:project-1:wiki/architecture.md".to_string(),
                    wiki_page: PathBuf::from("wiki/architecture.md"),
                    source_path: PathBuf::from("raw/architecture.md"),
                    result_type: SearchResultType::Wiki,
                    snippet: "The handler delegates routing.".to_string(),
                    score: 0.85,
                    sources: vec!["graph".to_string()],
                    explanations: Vec::new(),
                },
            ],
            vec!["shared_code_graph_unavailable".to_string()],
        ));

        assert!(output.degraded);
        assert_eq!(
            output.degraded_sources,
            vec!["shared_code_graph_unavailable".to_string()]
        );
        assert!(!output.truncated);
        assert!(output.truncated_components.is_empty());
        assert_eq!(output.code_citations.len(), 1);
        assert_eq!(output.code_citations[0].file, "src/handler.rs");
        assert_eq!(output.code_citations[0].line, None);
        assert_eq!(
            output.code_citations[0].symbol,
            Some("Request handler".to_string())
        );

        let prompt = synthesis_prompt(&output);
        assert!(prompt.contains("Code citations:"));
        assert!(prompt.contains("src/handler.rs"));
        assert!(prompt.contains("Request handler"));
    }

    #[test]
    fn ask_unified_graph_enrichment_uses_context_pack_code_edges_and_degrades() {
        let mut output = ask_output_from_search(SearchOutput::new(
            ScopeIdentity::project("project-1"),
            "Where is request handling wired?",
            10,
            vec![SearchResultOutput {
                title: Some("Architecture".to_string()),
                fusion_key: "project:project-1:wiki/architecture.md".to_string(),
                wiki_page: PathBuf::from("wiki/architecture.md"),
                source_path: PathBuf::from("raw/architecture.md"),
                result_type: SearchResultType::Wiki,
                snippet: "The handler delegates routing.".to_string(),
                score: 0.85,
                sources: vec!["bm25".to_string()],
                explanations: Vec::new(),
            }],
            Vec::new(),
        ));
        let scope = SearchScope::project("project-1");
        let facts = WikiGraphFacts {
            documents: vec![
                WikiGraphDocument {
                    scope: scope.clone(),
                    path: PathBuf::from("wiki/architecture.md"),
                    title: Some("Architecture".to_string()),
                },
                WikiGraphDocument {
                    scope: scope.clone(),
                    path: PathBuf::from("code/files/src/handler.rs.md"),
                    title: Some("Request handler".to_string()),
                },
            ],
            links: vec![WikiGraphLink {
                scope: scope.clone(),
                source_path: PathBuf::from("wiki/architecture.md"),
                raw_target: "handler".to_string(),
                target: WikiGraphLinkTarget::Resolved(PathBuf::from(
                    "code/files/src/handler.rs.md",
                )),
            }],
            sources: vec![WikiGraphSource {
                scope: scope.clone(),
                source_path: PathBuf::from("src/handler.rs"),
                document_path: PathBuf::from("code/files/src/handler.rs.md"),
            }],
            code_edges: vec![WikiGraphCodeEdge {
                scope: scope.clone(),
                document_path: PathBuf::from("code/files/src/handler.rs.md"),
                source: "src/handler.rs:handle".to_string(),
                target: "src/router.rs:route".to_string(),
                kind: "calls".to_string(),
                direction: "outgoing".to_string(),
                line: Some(42),
                provenance: "gcode_falkor".to_string(),
            }],
        };

        enrich_with_unified_graph_context(
            &mut output,
            &facts,
            vec![
                crate::falkor_graph::SHARED_CODE_GRAPH_TRUNCATED_SOURCE.to_string(),
                crate::falkor_graph::SHARED_CODE_GRAPH_TRUNCATED_SOURCE.to_string(),
            ],
            vec![
                "code_call_edges>7".to_string(),
                "code_call_edges>7".to_string(),
            ],
        );

        assert!(output.degraded);
        assert_eq!(
            output.degraded_sources,
            vec![crate::falkor_graph::SHARED_CODE_GRAPH_TRUNCATED_SOURCE.to_string()]
        );
        assert!(output.truncated);
        assert_eq!(
            output.truncated_components,
            vec!["code_call_edges>7".to_string()]
        );
        assert!(output.related_pages.iter().any(|page| {
            page.path == Path::new("code/files/src/handler.rs.md")
                && page.title.as_deref() == Some("Request handler")
        }));
        assert!(output.code_edges.iter().any(|edge| {
            edge.source == "src/handler.rs:handle"
                && edge.target == "src/router.rs:route"
                && edge.kind == "calls"
                && edge.direction == "outgoing"
                && edge.line == Some(42)
                && edge.provenance == "gcode_falkor"
        }));
        assert!(output.code_citations.iter().any(|citation| {
            citation.file == "src/handler.rs"
                && citation.line == Some(42)
                && citation.symbol.as_deref() == Some("handle")
        }));

        let prompt = synthesis_prompt(&output);
        assert!(prompt.contains("Unified graph context:"));
        assert!(prompt.contains("code/files/src/handler.rs.md"));
        assert!(prompt.contains("src/handler.rs:42 (handle)"));
    }
}

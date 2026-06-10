use std::collections::BTreeSet;

use super::{
    GraphExport, GraphExportEdge, GraphExportEdges, GraphExportOptions, WikiGraphFacts,
    WikiGraphLinkTarget, analytics, citation_node, code_endpoint_id, document_id, document_node,
    mermaid_label, mermaid_node_id, source_node, source_node_id, unresolved_target_id,
    unresolved_target_node,
};
use crate::graph::WikiGraphDocument;

impl WikiGraphFacts {
    pub fn export_graph(
        &self,
        options: GraphExportOptions,
    ) -> Result<GraphExport, analytics::GraphAnalyticsError> {
        let mut nodes = Vec::new();
        let mut node_ids = BTreeSet::new();
        let mut edges = GraphExportEdges::default();

        for document in &self.documents {
            let node = document_node(document);
            if node_ids.insert(node.id.clone()) {
                nodes.push(node);
            }
        }

        for source in &self.sources {
            let source_node = source_node(source);
            if node_ids.insert(source_node.id.clone()) {
                nodes.push(source_node);
            }

            let citation_node = citation_node(source);
            let citation_node_id = citation_node.id.clone();
            if node_ids.insert(citation_node_id.clone()) {
                nodes.push(citation_node);
            }

            edges.trust.push(GraphExportEdge {
                source: source_node_id(&source.scope, &source.source_path),
                target: document_id(&source.scope, &source.document_path),
                kind: "supports",
                raw_target: None,
            });
            edges.audit.push(GraphExportEdge {
                source: citation_node_id,
                target: source_node_id(&source.scope, &source.source_path),
                kind: "cites",
                raw_target: None,
            });
        }

        for link in &self.links {
            let target = match &link.target {
                WikiGraphLinkTarget::Resolved(path) => {
                    let node = document_node(&WikiGraphDocument {
                        scope: link.scope.clone(),
                        path: path.clone(),
                        title: None,
                    });
                    let node_id = node.id.clone();
                    if node_ids.insert(node_id.clone()) {
                        nodes.push(node);
                    }
                    document_id(&link.scope, path)
                }
                WikiGraphLinkTarget::Unresolved(target) => {
                    let node = unresolved_target_node(&link.scope, target);
                    let node_id = node.id.clone();
                    if node_ids.insert(node_id.clone()) {
                        nodes.push(node);
                    }
                    unresolved_target_id(&link.scope, target)
                }
            };
            edges.links.push(GraphExportEdge {
                source: document_id(&link.scope, &link.source_path),
                target,
                kind: "links",
                raw_target: Some(link.raw_target.clone()),
            });
        }

        for edge in &self.code_edges {
            let (kind, output_edges) = match edge.kind.as_str() {
                "imports" => ("imports", &mut edges.imports),
                "callers" => ("callers", &mut edges.callers),
                "calls" => ("calls", &mut edges.calls),
                other => {
                    log::warn!("unknown gwiki graph code edge kind `{other}`; exporting as calls");
                    ("calls", &mut edges.calls)
                }
            };
            output_edges.push(GraphExportEdge {
                source: code_endpoint_id(&edge.scope, &edge.source),
                target: code_endpoint_id(&edge.scope, &edge.target),
                kind,
                raw_target: Some(edge.provenance.clone()),
            });
        }

        let degraded = !options.degraded_sources.is_empty();
        Ok(GraphExport {
            command: "graph",
            degraded,
            degraded_sources: options.degraded_sources,
            analytics: analytics::analyze_facts(self)?,
            nodes,
            edges,
        })
    }
}

pub fn render_graph_report(export: &GraphExport) -> String {
    let mut report = String::from("# GWiki Graph Report\n\n");
    report.push_str(&format!("- Nodes: {}\n", export.nodes.len()));
    report.push_str(&format!(
        "- Edges: {}\n\n",
        export.edges.links.len()
            + export.edges.imports.len()
            + export.edges.calls.len()
            + export.edges.callers.len()
            + export.edges.trust.len()
            + export.edges.audit.len()
    ));

    report.push_str("## Degraded sources\n\n");
    if export.degraded_sources.is_empty() {
        report.push_str("- none\n\n");
    } else {
        for source in &export.degraded_sources {
            report.push_str(&format!("- {source}\n"));
        }
        report.push('\n');
    }

    report.push_str("## Analytics\n\n");
    report.push_str(&format!(
        "- Communities: {}\n",
        export.analytics.communities.len()
    ));
    if let Some(top) = export.analytics.centrality.first() {
        report.push_str(&format!(
            "- Top central node: {} (degree {})\n",
            top.node.id, top.degree
        ));
    } else {
        report.push_str("- Top central node: none\n");
    }
    report.push_str(&format!("- Bridges: {}\n", export.analytics.bridges.len()));
    report.push_str(&format!(
        "- Hotspots: {}\n\n",
        export.analytics.hotspots.len()
    ));

    report.push_str("## Overview\n\n```mermaid\ngraph LR\n");
    for node in &export.nodes {
        report.push_str(&format!(
            "    {}[\"{}\"]\n",
            mermaid_node_id(&node.id),
            mermaid_label(node)
        ));
    }
    for edge in export
        .edges
        .links
        .iter()
        .chain(export.edges.imports.iter())
        .chain(export.edges.calls.iter())
        .chain(export.edges.callers.iter())
        .chain(export.edges.trust.iter())
        .chain(export.edges.audit.iter())
    {
        report.push_str(&format!(
            "    {} --> {}\n",
            mermaid_node_id(&edge.source),
            mermaid_node_id(&edge.target)
        ));
    }
    report.push_str("```\n\n");

    report.push_str("## Edge classes\n\n");
    report.push_str(&format!("- links: {}\n", export.edges.links.len()));
    report.push_str(&format!("- imports: {}\n", export.edges.imports.len()));
    report.push_str(&format!("- calls: {}\n", export.edges.calls.len()));
    report.push_str(&format!("- callers: {}\n", export.edges.callers.len()));
    report.push_str(&format!("- trust: {}\n", export.edges.trust.len()));
    report.push_str(&format!("- audit: {}\n", export.edges.audit.len()));
    report
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use crate::search::SearchScope;

    use super::*;
    use crate::graph::{
        WikiGraphCodeEdge, WikiGraphDocument, WikiGraphLink, WikiGraphLinkTarget, WikiGraphSource,
    };

    #[test]
    fn export_graph_scopes_ids_and_emits_unresolved_target_nodes() {
        let project = SearchScope::project("project-1");
        let topic = SearchScope::topic("project-1");
        let facts = WikiGraphFacts {
            documents: vec![
                WikiGraphDocument {
                    scope: project.clone(),
                    path: "knowledge/topics/shared.md".into(),
                    title: Some("Shared".to_string()),
                },
                WikiGraphDocument {
                    scope: topic.clone(),
                    path: "knowledge/topics/shared.md".into(),
                    title: Some("Shared".to_string()),
                },
            ],
            links: vec![
                WikiGraphLink {
                    scope: project.clone(),
                    source_path: "knowledge/topics/shared.md".into(),
                    raw_target: "Missing".to_string(),
                    target: WikiGraphLinkTarget::Unresolved("Missing".to_string()),
                },
                WikiGraphLink {
                    scope: topic.clone(),
                    source_path: "knowledge/topics/shared.md".into(),
                    raw_target: "Missing".to_string(),
                    target: WikiGraphLinkTarget::Unresolved("Missing".to_string()),
                },
            ],
            sources: vec![WikiGraphSource {
                scope: project.clone(),
                source_path: "raw/source.md".into(),
                document_path: "knowledge/topics/shared.md".into(),
            }],
            code_edges: vec![
                WikiGraphCodeEdge {
                    scope: project.clone(),
                    document_path: PathBuf::from("knowledge/topics/shared.md"),
                    source: "src/lib.rs".to_string(),
                    target: "crate::main".to_string(),
                    kind: "imports".to_string(),
                    direction: "outgoing".to_string(),
                    line: None,
                    provenance: "test".to_string(),
                },
                WikiGraphCodeEdge {
                    scope: project.clone(),
                    document_path: PathBuf::from("knowledge/topics/shared.md"),
                    source: "src/lib.rs:run".to_string(),
                    target: "src/main.rs:main".to_string(),
                    kind: "calls".to_string(),
                    direction: "outgoing".to_string(),
                    line: Some(1),
                    provenance: "test".to_string(),
                },
                WikiGraphCodeEdge {
                    scope: SearchScope::project("project-1"),
                    document_path: PathBuf::from("knowledge/topics/shared.md"),
                    source: "src/main.rs:main".to_string(),
                    target: "src/lib.rs:run".to_string(),
                    kind: "callers".to_string(),
                    direction: "incoming".to_string(),
                    line: Some(2),
                    provenance: "test".to_string(),
                },
            ],
        };

        let export = facts
            .export_graph(GraphExportOptions::available())
            .expect("graph export");
        let node_ids = export
            .nodes
            .iter()
            .map(|node| node.id.as_str())
            .collect::<BTreeSet<_>>();
        let project_document = document_id(&project, &PathBuf::from("knowledge/topics/shared.md"));
        let topic_document = document_id(&topic, &PathBuf::from("knowledge/topics/shared.md"));
        let project_unresolved = unresolved_target_id(&project, "Missing");
        let topic_unresolved = unresolved_target_id(&topic, "Missing");
        let import_source = code_endpoint_id(&project, "src/lib.rs");
        let call_source = code_endpoint_id(&project, "src/lib.rs:run");
        let call_target = code_endpoint_id(&project, "src/main.rs:main");

        assert!(node_ids.contains(project_document.as_str()));
        assert!(node_ids.contains(topic_document.as_str()));
        assert!(node_ids.contains(project_unresolved.as_str()));
        assert!(node_ids.contains(topic_unresolved.as_str()));
        assert_eq!(export.edges.links[0].source, project_document);
        assert_eq!(export.edges.links[1].source, topic_document);
        assert_eq!(export.edges.imports[0].source, import_source);
        assert_eq!(export.edges.calls[0].source, call_source);
        assert_eq!(export.edges.calls[0].target, call_target);
        assert_eq!(export.edges.callers[0].source, call_target);
        assert_eq!(export.edges.callers[0].target, call_source);
        let report = render_graph_report(&export);
        assert!(report.contains(&format!(
            "{} --> {}",
            mermaid_node_id(&export.edges.imports[0].source),
            mermaid_node_id(&export.edges.imports[0].target)
        )));
        assert!(report.contains(&format!(
            "{} --> {}",
            mermaid_node_id(&export.edges.calls[0].source),
            mermaid_node_id(&export.edges.calls[0].target)
        )));
        assert!(report.contains(&format!(
            "{} --> {}",
            mermaid_node_id(&export.edges.callers[0].source),
            mermaid_node_id(&export.edges.callers[0].target)
        )));
        assert!(report.contains("- callers: 1"));
    }

    #[test]
    fn export_graph_adds_placeholder_for_missing_resolved_target() {
        let scope = SearchScope::project("project-1");
        let facts = WikiGraphFacts {
            documents: vec![WikiGraphDocument {
                scope: scope.clone(),
                path: "knowledge/topics/a.md".into(),
                title: Some("A".to_string()),
            }],
            links: vec![WikiGraphLink {
                scope: scope.clone(),
                source_path: "knowledge/topics/a.md".into(),
                raw_target: "B".to_string(),
                target: WikiGraphLinkTarget::Resolved("knowledge/topics/b.md".into()),
            }],
            sources: Vec::new(),
            code_edges: Vec::new(),
        };

        let export = facts
            .export_graph(GraphExportOptions::available())
            .expect("graph export");
        let target_id = document_id(&scope, &PathBuf::from("knowledge/topics/b.md"));

        assert!(export.nodes.iter().any(|node| {
            node.id == target_id
                && node.kind == "wiki_page"
                && node.path == "knowledge/topics/b.md"
                && node.title.is_none()
        }));
    }
}

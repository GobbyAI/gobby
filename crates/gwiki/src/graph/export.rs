use std::collections::BTreeSet;

use super::{
    GraphExport, GraphExportEdge, GraphExportEdges, GraphExportOptions, WikiGraphFacts,
    WikiGraphLinkTarget, analytics, citation_node, code_endpoint_id, document_id, document_node,
    mermaid_label, mermaid_node_id, source_node, source_node_id, unresolved_target_id,
    unresolved_target_node,
};

impl WikiGraphFacts {
    pub fn export_graph(&self, options: GraphExportOptions) -> GraphExport {
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
            if node_ids.insert(citation_node.id.clone()) {
                nodes.push(citation_node.clone());
            }

            edges.trust.push(GraphExportEdge {
                source: source_node_id(&source.scope, &source.source_path),
                target: document_id(&source.scope, &source.document_path),
                kind: "supports",
                raw_target: None,
            });
            edges.audit.push(GraphExportEdge {
                source: citation_node.id,
                target: source_node_id(&source.scope, &source.source_path),
                kind: "cites",
                raw_target: None,
            });
        }

        for link in &self.links {
            let target = match &link.target {
                WikiGraphLinkTarget::Resolved(path) => document_id(&link.scope, path),
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
            let graph_edge = GraphExportEdge {
                source: code_endpoint_id(&edge.scope, &edge.source),
                target: code_endpoint_id(&edge.scope, &edge.target),
                kind: if edge.kind == "imports" {
                    "imports"
                } else {
                    "calls"
                },
                raw_target: Some(edge.provenance.clone()),
            };
            if edge.kind == "imports" {
                edges.imports.push(graph_edge);
            } else {
                edges.calls.push(graph_edge);
            }
        }

        let degraded = !options.degraded_sources.is_empty();
        GraphExport {
            command: "graph",
            degraded,
            degraded_sources: options.degraded_sources,
            analytics: analytics::analyze_facts(self),
            nodes,
            edges,
        }
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
                    path: "wiki/shared.md".into(),
                    title: Some("Shared".to_string()),
                },
                WikiGraphDocument {
                    scope: topic.clone(),
                    path: "wiki/shared.md".into(),
                    title: Some("Shared".to_string()),
                },
            ],
            links: vec![
                WikiGraphLink {
                    scope: project.clone(),
                    source_path: "wiki/shared.md".into(),
                    raw_target: "Missing".to_string(),
                    target: WikiGraphLinkTarget::Unresolved("Missing".to_string()),
                },
                WikiGraphLink {
                    scope: topic.clone(),
                    source_path: "wiki/shared.md".into(),
                    raw_target: "Missing".to_string(),
                    target: WikiGraphLinkTarget::Unresolved("Missing".to_string()),
                },
            ],
            sources: vec![WikiGraphSource {
                scope: project.clone(),
                source_path: "raw/source.md".into(),
                document_path: "wiki/shared.md".into(),
            }],
            code_edges: vec![WikiGraphCodeEdge {
                scope: project,
                document_path: PathBuf::from("wiki/shared.md"),
                source: "src/lib.rs:run".to_string(),
                target: "src/main.rs:main".to_string(),
                kind: "calls".to_string(),
                direction: "outgoing".to_string(),
                line: Some(1),
                provenance: "test".to_string(),
            }],
        };

        let export = facts.export_graph(GraphExportOptions::available());
        let node_ids = export
            .nodes
            .iter()
            .map(|node| node.id.as_str())
            .collect::<BTreeSet<_>>();

        assert!(node_ids.contains("document:project:project-1:wiki/shared.md"));
        assert!(node_ids.contains("document:topic:project-1:wiki/shared.md"));
        assert!(node_ids.contains("unresolved:project:project-1:Missing"));
        assert!(node_ids.contains("unresolved:topic:project-1:Missing"));
        assert_eq!(
            export.edges.links[0].source,
            "document:project:project-1:wiki/shared.md"
        );
        assert_eq!(
            export.edges.links[1].source,
            "document:topic:project-1:wiki/shared.md"
        );
        assert_eq!(
            export.edges.calls[0].source,
            "code:project:project-1:src/lib.rs:run"
        );
        assert_eq!(
            export.edges.calls[0].target,
            "code:project:project-1:src/main.rs:main"
        );
    }
}

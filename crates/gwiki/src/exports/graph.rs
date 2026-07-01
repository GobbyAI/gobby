use std::path::Path;

use super::write::{export_relative_path, write_export_batch};
use super::{ExportArtifact, ExportKind, ExportRequest};
use crate::WikiError;
use crate::graph::{
    GraphExport, GraphExportNode, GraphExportOptions, WikiGraphFacts, render_graph_report,
};

pub fn export_graph_artifacts(
    root: &Path,
    facts: &WikiGraphFacts,
    options: GraphExportOptions,
) -> Result<Vec<ExportArtifact>, WikiError> {
    let export = facts.export_graph(options).map_err(graph_export_error)?;
    let graph_json = serde_json::to_string_pretty(&export).map_err(|error| WikiError::Json {
        action: "serialize graph export",
        path: None,
        source: error,
    })?;
    let report = render_graph_report(&export);
    write_export_batch(
        root,
        vec![
            ExportRequest {
                filename: "graph.json".to_string(),
                kind: ExportKind::Graph,
                contents: graph_json,
            },
            ExportRequest {
                filename: "GRAPH_REPORT.md".to_string(),
                kind: ExportKind::Report,
                contents: report,
            },
        ],
    )
}

pub(super) fn graph_export_error(error: crate::graph::analytics::GraphAnalyticsError) -> WikiError {
    WikiError::InvalidInput {
        field: "graph",
        message: error.to_string(),
    }
}

/// Emit the static agent-context exports: `graph.jsonld` (schema.org JSON-LD of
/// the vault document graph), `llms.txt` (portable index), and `llms-full.txt`
/// (portable content bundle). Reuses the existing `export_graph` vault export
/// and the vault Markdown on disk. Files are staged first, then committed as a
/// batch so a failed export preserves existing targets.
pub fn export_agent_artifacts(
    root: &Path,
    facts: &WikiGraphFacts,
    options: GraphExportOptions,
) -> Result<Vec<ExportArtifact>, WikiError> {
    let export = facts.export_graph(options).map_err(graph_export_error)?;

    let jsonld = render_graph_jsonld(&export)?;
    let llms_index = render_llms_index(&export);
    let llms_full = render_llms_full(root, &export);

    write_export_batch(
        root,
        vec![
            ExportRequest {
                filename: "graph.jsonld".to_string(),
                kind: ExportKind::Graph,
                contents: jsonld,
            },
            ExportRequest {
                filename: "llms.txt".to_string(),
                kind: ExportKind::Report,
                contents: llms_index,
            },
            ExportRequest {
                filename: "llms-full.txt".to_string(),
                kind: ExportKind::Bundle,
                contents: llms_full,
            },
        ],
    )
}

/// Render schema.org JSON-LD describing the gwiki **vault document graph**
/// (documents, sources, citations, and their wikilink / provenance edges).
/// Reuses the [`GraphExport`] node set produced by `export_graph`; the code
/// graph edge classes (`imports` / `calls` / `callers`) are intentionally
/// excluded — this describes the vault, not the code graph.
fn render_graph_jsonld(export: &GraphExport) -> Result<String, WikiError> {
    use std::collections::BTreeMap;

    // Relations that originate from each node id, keyed for stable lookup.
    let mut citations: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    let mut based_on: BTreeMap<&str, Vec<&str>> = BTreeMap::new();

    // Wikilinks: the linking document cites its target page.
    for edge in &export.edges.links {
        citations
            .entry(edge.source.as_str())
            .or_default()
            .push(edge.target.as_str());
    }
    // Audit: a citation node cites the source it was drawn from.
    for edge in &export.edges.audit {
        citations
            .entry(edge.source.as_str())
            .or_default()
            .push(edge.target.as_str());
    }
    // Trust: a document is based on the source that supports it.
    for edge in &export.edges.trust {
        based_on
            .entry(edge.target.as_str())
            .or_default()
            .push(edge.source.as_str());
    }

    let graph = export
        .nodes
        .iter()
        .map(|node| {
            let mut entity = serde_json::Map::new();
            entity.insert(
                "@id".to_string(),
                serde_json::Value::String(node.id.clone()),
            );
            entity.insert(
                "@type".to_string(),
                serde_json::Value::String(jsonld_type(node.kind).to_string()),
            );
            entity.insert(
                "name".to_string(),
                serde_json::Value::String(node_label(node)),
            );
            entity.insert(
                "url".to_string(),
                serde_json::Value::String(node.path.clone()),
            );
            entity.insert(
                "genre".to_string(),
                serde_json::Value::String(node.kind.to_string()),
            );
            if let Some(targets) = citations.get(node.id.as_str()) {
                entity.insert("citation".to_string(), id_references(targets));
            }
            if let Some(sources) = based_on.get(node.id.as_str()) {
                entity.insert("isBasedOn".to_string(), id_references(sources));
            }
            serde_json::Value::Object(entity)
        })
        .collect::<Vec<_>>();

    let document = serde_json::json!({
        "@context": "https://schema.org",
        "@graph": graph,
    });

    serde_json::to_string_pretty(&document).map_err(|error| WikiError::Json {
        action: "serialize graph jsonld",
        path: None,
        source: error,
    })
}

fn id_references(ids: &[&str]) -> serde_json::Value {
    serde_json::Value::Array(
        ids.iter()
            .map(|id| serde_json::json!({ "@id": id }))
            .collect(),
    )
}

/// Map a vault graph node kind onto a schema.org type.
fn jsonld_type(kind: &str) -> &'static str {
    match kind {
        "wiki_page" => "Article",
        "code" => "SoftwareSourceCode",
        "source" | "citation" | "unresolved_target" => "CreativeWork",
        _ => "DigitalDocument",
    }
}

/// Render an `llms.txt` portable index following the llmstxt.org convention:
/// an H1 title, a summary blockquote, then link sections for documents and
/// sources. Built from the same [`GraphExport`] node set.
fn render_llms_index(export: &GraphExport) -> String {
    let documents = document_nodes(export);
    let sources = export
        .nodes
        .iter()
        .filter(|node| node.kind == "source")
        .collect::<Vec<_>>();

    let mut out = String::from("# GWiki Vault Index\n\n");
    out.push_str(&format!(
        "> Static agent index for {}. {} documents, {} sources.\n\n",
        scope_label(export),
        documents.len(),
        sources.len()
    ));

    push_link_section(&mut out, "Documents", &documents);
    out.push('\n');
    push_link_section(&mut out, "Sources", &sources);

    out
}

/// Render an `llms-full.txt` portable content bundle: the full Markdown body of
/// every vault document, concatenated in graph order. Bodies are read from the
/// vault on disk; a missing or unreadable file degrades to a placeholder rather
/// than failing the export.
fn render_llms_full(root: &Path, export: &GraphExport) -> String {
    let documents = document_nodes(export);

    let mut out = String::from("# GWiki Vault Content\n\n");
    out.push_str(&format!(
        "> Full content export for {}. {} documents.\n\n",
        scope_label(export),
        documents.len()
    ));

    for node in documents {
        out.push_str(&format!("## {}\n\n", node_label(node)));
        out.push_str(&format!("`{}`\n\n", node.path));
        match vault_document_contents(root, &node.path) {
            Some(contents) => {
                out.push_str(contents.trim_end());
                out.push_str("\n\n");
            }
            None => out.push_str("_(content unavailable)_\n\n"),
        }
        out.push_str("---\n\n");
    }

    out
}

/// Documents in the vault graph (wiki pages, code mirrors, and other documents),
/// excluding source / citation / unresolved-target nodes.
fn document_nodes(export: &GraphExport) -> Vec<&GraphExportNode> {
    export
        .nodes
        .iter()
        .filter(|node| is_document_node(node.kind))
        .collect()
}

fn is_document_node(kind: &str) -> bool {
    matches!(kind, "wiki_page" | "code" | "document")
}

fn push_link_section(out: &mut String, heading: &str, nodes: &[&GraphExportNode]) {
    out.push_str(&format!("## {heading}\n\n"));
    if nodes.is_empty() {
        out.push_str("- _(none)_\n");
        return;
    }
    for node in nodes {
        out.push_str(&format!("- [{}]({})\n", node_label(node), node.path));
    }
}

fn node_label(node: &GraphExportNode) -> String {
    node.title.clone().unwrap_or_else(|| node.path.clone())
}

fn scope_label(export: &GraphExport) -> String {
    match export.nodes.first() {
        Some(node) => format!("{} {}", node.scope_kind, node.scope_id),
        None => "the vault".to_string(),
    }
}

/// Read a vault document body, rejecting absolute or traversing paths.
fn vault_document_contents(root: &Path, path: &str) -> Option<String> {
    let root = root.canonicalize().ok()?;
    let relative = export_relative_path(path).ok()?;
    let resolved = root.join(relative).canonicalize().ok()?;
    if !resolved.starts_with(&root) {
        return None;
    }
    std::fs::read_to_string(resolved).ok()
}

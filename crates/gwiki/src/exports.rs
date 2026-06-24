use std::fs;
use std::io::{Error, ErrorKind};
use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::graph::{
    GraphExport, GraphExportNode, GraphExportOptions, WikiGraphFacts, render_graph_report,
};
use crate::{ScopeIdentity, WikiError};

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExportKind {
    Bundle,
    Graph,
    Report,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExportRequest {
    pub filename: String,
    pub kind: ExportKind,
    pub contents: String,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct ExportArtifact {
    pub path: PathBuf,
    pub kind: ExportKind,
    pub bytes_written: usize,
}

#[derive(Debug)]
struct StagedExport {
    artifact: ExportArtifact,
    temp_path: PathBuf,
    backup_path: Option<PathBuf>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExportCommand {
    WorkflowAssets {
        filename: String,
    },
    ReportFile {
        filename: String,
        source_path: PathBuf,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct ExportOutput {
    pub command: &'static str,
    pub scope: ScopeIdentity,
    pub artifacts: Vec<ExportArtifact>,
}

impl ExportOutput {
    pub fn new(scope: ScopeIdentity, artifacts: Vec<ExportArtifact>) -> Self {
        Self {
            command: "export",
            scope,
            artifacts,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WorkflowAsset {
    pub name: &'static str,
    pub filename: &'static str,
    pub contents: &'static str,
}

const WORKFLOW_ASSETS: &[WorkflowAsset] = &[
    WorkflowAsset {
        name: "compile",
        filename: "compile.md",
        contents: include_str!("../assets/skills/compile.md"),
    },
    WorkflowAsset {
        name: "query",
        filename: "query.md",
        contents: include_str!("../assets/skills/query.md"),
    },
    WorkflowAsset {
        name: "audit",
        filename: "audit.md",
        contents: include_str!("../assets/skills/audit.md"),
    },
];

pub fn bundled_workflow_assets() -> &'static [WorkflowAsset] {
    WORKFLOW_ASSETS
}

pub fn run(root: &Path, command: ExportCommand) -> Result<Vec<ExportArtifact>, WikiError> {
    match command {
        ExportCommand::WorkflowAssets { filename } => {
            export_workflow_assets(root, filename).map(|artifact| vec![artifact])
        }
        ExportCommand::ReportFile {
            filename,
            source_path,
        } => export_report_file(root, filename, source_path).map(|artifact| vec![artifact]),
    }
}

pub fn export_workflow_assets(
    root: &Path,
    filename: impl Into<String>,
) -> Result<ExportArtifact, WikiError> {
    write_export(
        root,
        ExportRequest {
            filename: filename.into(),
            kind: ExportKind::Bundle,
            contents: workflow_assets_bundle(),
        },
    )
}

pub fn export_report_file(
    root: &Path,
    filename: impl Into<String>,
    source_path: impl AsRef<Path>,
) -> Result<ExportArtifact, WikiError> {
    let source_path = source_path.as_ref();
    let contents = std::fs::read_to_string(source_path).map_err(|error| WikiError::Io {
        action: "read export report",
        path: Some(source_path.to_path_buf()),
        source: error,
    })?;
    write_export(
        root,
        ExportRequest {
            filename: filename.into(),
            kind: ExportKind::Report,
            contents,
        },
    )
}

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

fn graph_export_error(error: crate::graph::analytics::GraphAnalyticsError) -> WikiError {
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
    let relative = export_relative_path(path).ok()?;
    std::fs::read_to_string(root.join(relative)).ok()
}

pub fn export_markdown_report(
    root: &Path,
    filename: impl Into<String>,
    contents: String,
) -> Result<ExportArtifact, WikiError> {
    write_export(
        root,
        ExportRequest {
            filename: filename.into(),
            kind: ExportKind::Report,
            contents,
        },
    )
}

pub fn write_export(root: &Path, request: ExportRequest) -> Result<ExportArtifact, WikiError> {
    let mut artifacts = write_export_batch(root, vec![request])?;
    Ok(artifacts.remove(0))
}

fn write_export_batch(
    root: &Path,
    requests: Vec<ExportRequest>,
) -> Result<Vec<ExportArtifact>, WikiError> {
    let mut staged = Vec::with_capacity(requests.len());
    for (sequence, request) in requests.into_iter().enumerate() {
        match stage_export(root, request, sequence) {
            Ok(export) => staged.push(export),
            Err(error) => {
                cleanup_staged_exports(&staged);
                return Err(error);
            }
        }
    }
    if let Err(error) = commit_staged_exports(&mut staged) {
        rollback_staged_exports(&staged);
        return Err(error);
    }
    Ok(staged.into_iter().map(|export| export.artifact).collect())
}

fn stage_export(
    root: &Path,
    request: ExportRequest,
    sequence: usize,
) -> Result<StagedExport, WikiError> {
    let relative_path = export_relative_path(&request.filename)?;
    let path = root.join("outputs").join(relative_path);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create export directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    if path.is_dir() {
        return Err(WikiError::Io {
            action: "write export",
            path: Some(path),
            source: Error::new(ErrorKind::AlreadyExists, "export target is a directory"),
        });
    }

    let temp_path = export_sidecar_path(&path, "tmp", sequence);
    std::fs::write(&temp_path, &request.contents).map_err(|error| WikiError::Io {
        action: "write export",
        path: Some(temp_path.clone()),
        source: error,
    })?;

    Ok(StagedExport {
        artifact: ExportArtifact {
            path,
            kind: request.kind,
            bytes_written: request.contents.len(),
        },
        temp_path,
        backup_path: None,
    })
}

fn commit_staged_exports(staged: &mut [StagedExport]) -> Result<(), WikiError> {
    for (sequence, export) in staged.iter_mut().enumerate() {
        let target = &export.artifact.path;
        if target.exists() {
            if target.is_dir() {
                return Err(WikiError::Io {
                    action: "write export",
                    path: Some(target.clone()),
                    source: Error::new(ErrorKind::AlreadyExists, "export target is a directory"),
                });
            }
            let backup_path = export_sidecar_path(target, "backup", sequence);
            fs::rename(target, &backup_path).map_err(|error| WikiError::Io {
                action: "backup export",
                path: Some(target.clone()),
                source: error,
            })?;
            export.backup_path = Some(backup_path);
        }
    }

    for export in staged.iter() {
        fs::rename(&export.temp_path, &export.artifact.path).map_err(|error| WikiError::Io {
            action: "commit export",
            path: Some(export.artifact.path.clone()),
            source: error,
        })?;
    }

    for export in staged.iter() {
        if let Some(backup_path) = &export.backup_path {
            let _ = fs::remove_file(backup_path);
        }
    }
    Ok(())
}

fn cleanup_staged_exports(staged: &[StagedExport]) {
    for export in staged {
        let _ = fs::remove_file(&export.temp_path);
    }
}

fn rollback_staged_exports(staged: &[StagedExport]) {
    for export in staged {
        let _ = fs::remove_file(&export.temp_path);
        if export.backup_path.is_none() {
            let _ = fs::remove_file(&export.artifact.path);
        }
    }
    for export in staged {
        if let Some(backup_path) = &export.backup_path {
            let _ = fs::remove_file(&export.artifact.path);
            let _ = fs::rename(backup_path, &export.artifact.path);
        }
    }
}

fn export_sidecar_path(path: &Path, kind: &str, sequence: usize) -> PathBuf {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    let file_name = path
        .file_name()
        .map(|name| name.to_string_lossy())
        .unwrap_or_else(|| "export".into());
    path.with_file_name(format!(".{file_name}.{kind}.{unique}.{sequence}"))
}

fn export_relative_path(filename: &str) -> Result<PathBuf, WikiError> {
    let path = Path::new(filename);
    if filename.trim().is_empty() || path.is_absolute() {
        return Err(invalid_export_filename(filename));
    }

    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Normal(part) => normalized.push(part),
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                return Err(invalid_export_filename(filename));
            }
        }
    }

    if normalized.as_os_str().is_empty() {
        return Err(invalid_export_filename(filename));
    }

    Ok(normalized)
}

fn invalid_export_filename(filename: &str) -> WikiError {
    WikiError::InvalidInput {
        field: "filename",
        message: format!("export filename must stay under outputs/: {filename}"),
    }
}

fn workflow_assets_bundle() -> String {
    let mut contents = String::from("# GWiki Workflow Assets\n\n");
    for asset in bundled_workflow_assets() {
        contents.push_str("## ");
        contents.push_str(asset.name);
        contents.push_str("\n\n");
        contents.push_str("Source asset: `");
        contents.push_str(asset.filename);
        contents.push_str("`\n\n");
        contents.push_str(asset.contents.trim_end());
        contents.push_str("\n\n");
    }
    contents
}

#[cfg(test)]
mod tests {
    use std::fs;

    use crate::graph::analytics::GraphAnalyticsError;
    use crate::graph::{
        GraphExportOptions, WikiGraphCodeEdge, WikiGraphDocument, WikiGraphFacts, WikiGraphLink,
        WikiGraphLinkTarget, WikiGraphSource,
    };
    use crate::search::SearchScope;

    use super::*;

    #[test]
    fn exports_do_not_mutate_wiki_state() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let wiki_page = root.join("knowledge/topics/ownership.md");
        fs::create_dir_all(wiki_page.parent().expect("wiki parent")).expect("wiki dir");
        fs::write(&wiki_page, "# Ownership\n\nCanonical page.\n").expect("wiki page");
        let before = fs::read_to_string(&wiki_page).expect("read before");

        let artifact = write_export(
            root,
            ExportRequest {
                filename: "ownership-bundle.md".to_string(),
                kind: ExportKind::Bundle,
                contents: "# Ownership Bundle\n\nGenerated export.\n".to_string(),
            },
        )
        .expect("export is written");

        assert_eq!(artifact.path, root.join("outputs/ownership-bundle.md"));
        assert_eq!(artifact.bytes_written, 38);
        assert_eq!(fs::read_to_string(&wiki_page).expect("read after"), before);
        assert_eq!(
            fs::read_to_string(root.join("outputs/ownership-bundle.md")).expect("export"),
            "# Ownership Bundle\n\nGenerated export.\n"
        );
        assert_eq!(
            bundled_workflow_assets()
                .iter()
                .map(|asset| asset.name)
                .collect::<Vec<_>>(),
            vec!["compile", "query", "audit"]
        );

        let bundle_artifact =
            export_workflow_assets(root, "workflow-assets.md").expect("workflow assets export");
        assert_eq!(
            bundle_artifact.path,
            root.join("outputs/workflow-assets.md")
        );
        let bundle = fs::read_to_string(root.join("outputs/workflow-assets.md")).expect("bundle");
        assert!(bundle.contains("# GWiki Workflow Assets"));
        assert!(bundle.contains("## compile"));

        let report = root.join("meta/health/latest.md");
        fs::create_dir_all(report.parent().expect("report parent")).expect("report dir");
        fs::write(&report, "# Health\n\nGenerated report.\n").expect("report");
        let report_artifact =
            export_report_file(root, "reports/health.md", &report).expect("report export");
        assert_eq!(report_artifact.path, root.join("outputs/reports/health.md"));
        assert_eq!(
            fs::read_to_string(root.join("outputs/reports/health.md")).expect("report export"),
            "# Health\n\nGenerated report.\n"
        );
        assert_eq!(fs::read_to_string(&wiki_page).expect("read final"), before);
    }

    #[test]
    fn graph_analytics_export_artifacts_include_degradation_and_mermaid() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let scope = SearchScope::project("project-123");
        let facts = WikiGraphFacts {
            documents: vec![
                WikiGraphDocument {
                    scope: scope.clone(),
                    path: "knowledge/topics/overview.md".into(),
                    title: Some("Overview".to_string()),
                },
                WikiGraphDocument {
                    scope: scope.clone(),
                    path: "code/src/lib.rs".into(),
                    title: Some("lib.rs".to_string()),
                },
                WikiGraphDocument {
                    scope: scope.clone(),
                    path: "documents/design.md".into(),
                    title: Some("Design Notes".to_string()),
                },
            ],
            links: vec![WikiGraphLink {
                scope: scope.clone(),
                source_path: "knowledge/topics/overview.md".into(),
                raw_target: "code/src/lib.rs".to_string(),
                target: WikiGraphLinkTarget::Resolved("code/src/lib.rs".into()),
            }],
            sources: vec![WikiGraphSource {
                scope,
                source_path: "raw/sources/example.md".into(),
                document_path: "knowledge/topics/overview.md".into(),
            }],
            code_edges: Vec::new(),
        };

        let artifacts = export_graph_artifacts(
            root,
            &facts,
            GraphExportOptions::degraded(vec![
                "falkordb_unavailable".to_string(),
                "semantic_relations_unavailable".to_string(),
            ]),
        )
        .expect("graph artifacts exported");

        assert_eq!(artifacts.len(), 2);
        assert_eq!(artifacts[0].path, root.join("outputs/graph.json"));
        assert_eq!(artifacts[1].path, root.join("outputs/GRAPH_REPORT.md"));

        let graph_json: serde_json::Value = serde_json::from_str(
            &fs::read_to_string(root.join("outputs/graph.json")).expect("graph json"),
        )
        .expect("valid graph json");
        assert_eq!(graph_json["degraded"], true);
        assert_eq!(
            graph_json["degraded_sources"],
            serde_json::json!(["falkordb_unavailable", "semantic_relations_unavailable"])
        );
        assert_eq!(graph_json["nodes"].as_array().expect("nodes").len(), 5);
        assert_eq!(
            graph_json["edges"]["links"]
                .as_array()
                .expect("links")
                .len(),
            1
        );
        assert_eq!(
            graph_json["edges"]["imports"].as_array().expect("imports"),
            &Vec::<serde_json::Value>::new()
        );
        assert_eq!(
            graph_json["edges"]["calls"].as_array().expect("calls"),
            &Vec::<serde_json::Value>::new()
        );
        assert_eq!(
            graph_json["edges"]["trust"]
                .as_array()
                .expect("trust")
                .len(),
            1
        );
        assert_eq!(
            graph_json["edges"]["audit"]
                .as_array()
                .expect("audit")
                .len(),
            1
        );
        let central_node_id = graph_json["analytics"]["centrality"][0]["node"]["id"]
            .as_str()
            .expect("central node id");
        assert!(central_node_id.starts_with("document-knowledge-topics-overview-md-"));
        assert_eq!(
            graph_json["analytics"]["centrality"][0]["degree"],
            serde_json::json!(2)
        );
        // Weighted Leiden clusters the citation→source→overview→lib.rs chain
        // into two 2-node communities (split at the middle "supports" edge),
        // plus the isolated design.md singleton — three communities total. The
        // first (sorted by smallest member id) is the {citation, source} pair.
        assert_eq!(
            graph_json["analytics"]["communities"][0]["nodes"]
                .as_array()
                .expect("community nodes")
                .len(),
            2
        );

        let report =
            fs::read_to_string(root.join("outputs/GRAPH_REPORT.md")).expect("graph report");
        assert!(report.contains("# GWiki Graph Report"));
        assert!(report.contains("## Analytics"));
        assert!(report.contains(&format!("- Top central node: {central_node_id} (degree 2)")));
        assert!(report.contains("- Communities: 3"));
        assert!(report.contains("## Degraded sources"));
        assert!(report.contains("- falkordb_unavailable"));
        assert!(report.contains("```mermaid"));
        assert!(report.contains(" --> "));
    }

    #[test]
    fn graph_export_removes_json_when_report_write_fails() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let outputs = root.join("outputs");
        fs::create_dir_all(&outputs).expect("outputs dir");
        fs::write(outputs.join("graph.json"), "existing graph").expect("existing graph");
        fs::create_dir(outputs.join("GRAPH_REPORT.md")).expect("blocking dir");

        let error = export_graph_artifacts(
            root,
            &WikiGraphFacts::default(),
            GraphExportOptions::available(),
        )
        .expect_err("report path directory should fail");

        assert!(error.to_string().contains("GRAPH_REPORT.md"));
        assert_eq!(
            fs::read_to_string(outputs.join("graph.json")).expect("preserved graph"),
            "existing graph"
        );
    }

    #[test]
    fn graph_export_errors_are_invalid_input() {
        let error = graph_export_error(GraphAnalyticsError::DuplicateNode {
            id: "node-1".to_string(),
            existing_kind: "topic".to_string(),
            duplicate_kind: "source".to_string(),
            existing_weight: 1.0,
            duplicate_weight: 0.5,
        });

        assert!(matches!(
            error,
            WikiError::InvalidInput {
                field: "graph",
                message
            } if message.contains("duplicate graph node `node-1`")
        ));
    }

    fn agent_export_facts(scope: SearchScope) -> WikiGraphFacts {
        WikiGraphFacts {
            documents: vec![
                WikiGraphDocument {
                    scope: scope.clone(),
                    path: "knowledge/topics/overview.md".into(),
                    title: Some("Overview".to_string()),
                },
                WikiGraphDocument {
                    scope: scope.clone(),
                    path: "code/src/lib.rs".into(),
                    title: Some("lib.rs".to_string()),
                },
                WikiGraphDocument {
                    scope: scope.clone(),
                    path: "documents/design.md".into(),
                    title: Some("Design Notes".to_string()),
                },
            ],
            links: vec![WikiGraphLink {
                scope: scope.clone(),
                source_path: "knowledge/topics/overview.md".into(),
                raw_target: "[[Design Notes]]".to_string(),
                target: WikiGraphLinkTarget::Resolved("documents/design.md".into()),
            }],
            sources: vec![WikiGraphSource {
                scope: scope.clone(),
                source_path: "raw/sources/example.md".into(),
                document_path: "knowledge/topics/overview.md".into(),
            }],
            code_edges: vec![WikiGraphCodeEdge {
                scope,
                document_path: "code/src/lib.rs".into(),
                source: "alpha".to_string(),
                target: "beta".to_string(),
                kind: "imports".to_string(),
                direction: "out".to_string(),
                line: Some(3),
                provenance: "code-edge-must-be-excluded".to_string(),
            }],
        }
    }

    #[test]
    fn agent_exports_emit_index_jsonld_and_content() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let overview = root.join("knowledge/topics/overview.md");
        fs::create_dir_all(overview.parent().expect("overview parent")).expect("overview dir");
        fs::write(&overview, "# Overview\n\nVault entrypoint.\n").expect("overview page");

        let artifacts = export_agent_artifacts(
            root,
            &agent_export_facts(SearchScope::project("project-123")),
            GraphExportOptions::available(),
        )
        .expect("agent artifacts exported");

        assert_eq!(
            artifacts
                .iter()
                .map(|artifact| artifact.path.clone())
                .collect::<Vec<_>>(),
            vec![
                root.join("outputs/graph.jsonld"),
                root.join("outputs/llms.txt"),
                root.join("outputs/llms-full.txt"),
            ]
        );

        // graph.jsonld: schema.org JSON-LD of the vault document graph.
        let jsonld_text =
            fs::read_to_string(root.join("outputs/graph.jsonld")).expect("graph jsonld");
        let jsonld: serde_json::Value =
            serde_json::from_str(&jsonld_text).expect("valid graph jsonld");
        assert_eq!(jsonld["@context"], "https://schema.org");
        let graph = jsonld["@graph"].as_array().expect("@graph array");
        // Vault nodes only: 3 documents + 1 source + 1 citation. No code endpoints.
        assert_eq!(graph.len(), 5);

        let overview_entity = graph
            .iter()
            .find(|entity| entity["url"] == serde_json::json!("knowledge/topics/overview.md"))
            .expect("overview entity");
        assert_eq!(overview_entity["@type"], "Article");
        assert_eq!(overview_entity["genre"], "wiki_page");
        assert_eq!(overview_entity["name"], "Overview");
        // Wikilink → cites the resolved target page.
        let design_id = graph
            .iter()
            .find(|entity| entity["url"] == serde_json::json!("documents/design.md"))
            .expect("design entity")["@id"]
            .clone();
        assert_eq!(
            overview_entity["citation"],
            serde_json::json!([{ "@id": design_id }])
        );
        // Source provenance → isBasedOn the supporting source.
        let source_id = graph
            .iter()
            .find(|entity| entity["genre"] == serde_json::json!("source"))
            .expect("source entity")["@id"]
            .clone();
        assert_eq!(
            overview_entity["isBasedOn"],
            serde_json::json!([{ "@id": source_id }])
        );

        let lib_entity = graph
            .iter()
            .find(|entity| entity["url"] == serde_json::json!("code/src/lib.rs"))
            .expect("lib entity");
        assert_eq!(lib_entity["@type"], "SoftwareSourceCode");

        // The code graph is excluded: no code-edge endpoints or edge classes.
        assert!(!jsonld_text.contains("code-edge-must-be-excluded"));
        assert!(!jsonld_text.contains("\"imports\""));
        assert!(!jsonld_text.contains("\"calls\""));

        // llms.txt: portable index with link sections.
        let index = fs::read_to_string(root.join("outputs/llms.txt")).expect("llms index");
        assert!(index.starts_with("# GWiki Vault Index"));
        assert!(
            index.contains("> Static agent index for project project-123. 3 documents, 1 sources.")
        );
        assert!(index.contains("## Documents"));
        assert!(index.contains("- [Overview](knowledge/topics/overview.md)"));
        assert!(index.contains("- [Design Notes](documents/design.md)"));
        assert!(index.contains("## Sources"));
        assert!(index.contains("- [raw/sources/example.md](raw/sources/example.md)"));

        // llms-full.txt: real content for present docs, placeholder for missing ones.
        let full = fs::read_to_string(root.join("outputs/llms-full.txt")).expect("llms full");
        assert!(full.starts_with("# GWiki Vault Content"));
        assert!(full.contains("## Overview"));
        assert!(full.contains("`knowledge/topics/overview.md`"));
        assert!(full.contains("Vault entrypoint."));
        // design.md was never written to disk, so it degrades gracefully.
        assert!(full.contains("## Design Notes"));
        assert!(full.contains("_(content unavailable)_"));
    }

    #[test]
    fn agent_exports_do_not_mutate_vault() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let overview = root.join("knowledge/topics/overview.md");
        fs::create_dir_all(overview.parent().expect("overview parent")).expect("overview dir");
        fs::write(&overview, "# Overview\n\nVault entrypoint.\n").expect("overview page");
        let before = fs::read_to_string(&overview).expect("read before");

        export_agent_artifacts(
            root,
            &agent_export_facts(SearchScope::project("project-123")),
            GraphExportOptions::available(),
        )
        .expect("agent artifacts exported");

        assert_eq!(fs::read_to_string(&overview).expect("read after"), before);
    }

    #[test]
    fn agent_exports_clean_up_on_write_failure() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let outputs = root.join("outputs");
        fs::create_dir_all(&outputs).expect("outputs dir");
        fs::write(outputs.join("graph.jsonld"), "existing graph").expect("existing graph");
        // A directory at the llms.txt path forces the second write to fail.
        fs::create_dir(outputs.join("llms.txt")).expect("blocking dir");

        let error = export_agent_artifacts(
            root,
            &agent_export_facts(SearchScope::project("project-123")),
            GraphExportOptions::available(),
        )
        .expect_err("blocked llms.txt path should fail");

        assert!(error.to_string().contains("llms.txt"));
        assert_eq!(
            fs::read_to_string(outputs.join("graph.jsonld")).expect("preserved graph"),
            "existing graph"
        );
    }
}

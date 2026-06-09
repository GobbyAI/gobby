use std::fs;
use std::path::{Component, Path, PathBuf};

use crate::graph::{GraphExportOptions, WikiGraphFacts, render_graph_report};
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
        name: "research",
        filename: "research.md",
        contents: include_str!("../assets/skills/research.md"),
    },
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
    let graph_artifact = write_export(
        root,
        ExportRequest {
            filename: "graph.json".to_string(),
            kind: ExportKind::Graph,
            contents: graph_json,
        },
    )?;
    let report_artifact = match write_export(
        root,
        ExportRequest {
            filename: "GRAPH_REPORT.md".to_string(),
            kind: ExportKind::Report,
            contents: report,
        },
    ) {
        Ok(artifact) => artifact,
        Err(error) => {
            let _ = fs::remove_file(&graph_artifact.path);
            return Err(error);
        }
    };
    Ok(vec![graph_artifact, report_artifact])
}

fn graph_export_error(error: crate::graph::analytics::GraphAnalyticsError) -> WikiError {
    WikiError::InvalidInput {
        field: "graph",
        message: error.to_string(),
    }
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
    let relative_path = export_relative_path(&request.filename)?;
    let path = root.join("outputs").join(relative_path);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create export directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }

    std::fs::write(&path, &request.contents).map_err(|error| WikiError::Io {
        action: "write export",
        path: Some(path.clone()),
        source: error,
    })?;

    Ok(ExportArtifact {
        path,
        kind: request.kind,
        bytes_written: request.contents.len(),
    })
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
        GraphExportOptions, WikiGraphDocument, WikiGraphFacts, WikiGraphLink, WikiGraphLinkTarget,
        WikiGraphSource,
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
            vec!["research", "compile", "query", "audit"]
        );

        let bundle_artifact =
            export_workflow_assets(root, "workflow-assets.md").expect("workflow assets export");
        assert_eq!(
            bundle_artifact.path,
            root.join("outputs/workflow-assets.md")
        );
        let bundle = fs::read_to_string(root.join("outputs/workflow-assets.md")).expect("bundle");
        assert!(bundle.contains("# GWiki Workflow Assets"));
        assert!(bundle.contains("## research"));

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
        assert_eq!(
            graph_json["analytics"]["communities"][0]["nodes"]
                .as_array()
                .expect("community nodes")
                .len(),
            1
        );

        let report =
            fs::read_to_string(root.join("outputs/GRAPH_REPORT.md")).expect("graph report");
        assert!(report.contains("# GWiki Graph Report"));
        assert!(report.contains("## Analytics"));
        assert!(report.contains(&format!("- Top central node: {central_node_id} (degree 2)")));
        assert!(report.contains("- Communities: 5"));
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
        fs::create_dir(outputs.join("GRAPH_REPORT.md")).expect("blocking dir");

        let error = export_graph_artifacts(
            root,
            &WikiGraphFacts::default(),
            GraphExportOptions::available(),
        )
        .expect_err("report path directory should fail");

        assert!(error.to_string().contains("GRAPH_REPORT.md"));
        assert!(!outputs.join("graph.json").exists());
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
}

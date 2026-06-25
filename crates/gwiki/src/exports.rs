use std::path::{Path, PathBuf};

use crate::{ScopeIdentity, WikiError};

mod assets;
mod graph;
mod write;

pub use assets::{
    WorkflowAsset, bundled_workflow_assets, export_markdown_report, export_report_file,
    export_workflow_assets,
};
pub use graph::{export_agent_artifacts, export_graph_artifacts};
pub use write::write_export;

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

#[cfg(test)]
mod tests;

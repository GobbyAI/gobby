use std::path::Path;

use super::write::write_export;
use super::{ExportArtifact, ExportKind, ExportRequest};
use crate::WikiError;

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
        contents: include_str!("../../assets/skills/compile.md"),
    },
    WorkflowAsset {
        name: "query",
        filename: "query.md",
        contents: include_str!("../../assets/skills/query.md"),
    },
    WorkflowAsset {
        name: "audit",
        filename: "audit.md",
        contents: include_str!("../../assets/skills/audit.md"),
    },
];

pub fn bundled_workflow_assets() -> &'static [WorkflowAsset] {
    WORKFLOW_ASSETS
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

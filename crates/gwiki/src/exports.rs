use std::path::{Component, Path, PathBuf};

use crate::{ScopeIdentity, WikiError};

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ExportKind {
    Bundle,
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
        source: error.to_string(),
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

pub fn write_export(root: &Path, request: ExportRequest) -> Result<ExportArtifact, WikiError> {
    let relative_path = export_relative_path(&request.filename)?;
    let path = root.join("outputs").join(relative_path);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create export directory",
            path: Some(parent.to_path_buf()),
            source: error.to_string(),
        })?;
    }

    std::fs::write(&path, &request.contents).map_err(|error| WikiError::Io {
        action: "write export",
        path: Some(path.clone()),
        source: error.to_string(),
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

    use super::*;

    #[test]
    fn exports_do_not_mutate_wiki_state() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let wiki_page = root.join("wiki/topics/ownership.md");
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
}

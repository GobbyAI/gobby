use std::path::{Component, Path, PathBuf};

use crate::WikiError;

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
    }
}

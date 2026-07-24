use std::fs;
use std::path::{Component, Path, PathBuf};

use crate::WikiError;
use crate::sources::SourceRecord;

pub(crate) fn validate_source_id(id: &str) -> Result<&str, WikiError> {
    let id = id.trim();
    if id.is_empty()
        || id.contains("..")
        || id.contains('/')
        || id.contains('\\')
        || Path::new(id)
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(WikiError::InvalidInput {
            field: "source_id",
            message: format!("unsafe source id `{id}`"),
        });
    }
    // Source ids become `raw/<id>.md` and `knowledge/sources/<id>.md`
    // filenames, so an id like `claude` would shadow CLAUDE.md instruction
    // lookup on case-insensitive filesystems (#17645).
    if gobby_core::vault::reserved::is_reserved_instruction_stem(id) {
        return Err(WikiError::InvalidInput {
            field: "source_id",
            message: format!("source id `{id}` collides with an agent instruction filename"),
        });
    }
    Ok(id)
}

pub(crate) fn raw_source_path(id: &str) -> Result<PathBuf, WikiError> {
    let id = validate_source_id(id)?;
    Ok(Path::new("raw").join(format!("{id}.md")))
}

pub(crate) fn derived_markdown_path(record: &SourceRecord) -> Result<PathBuf, WikiError> {
    let id = validate_source_id(&record.id)?;
    Ok(PathBuf::from("knowledge")
        .join("sources")
        .join(format!("{id}.md")))
}

/// Vault-relative paths of stored assets for a source id: `raw/assets/<id>.<ext>`
/// entries with exactly one extension segment (derived assets such as
/// `<id>.pdf.png` belong to other pipelines and are skipped).
pub(crate) fn source_asset_paths_for_id(
    vault_root: &Path,
    id: &str,
) -> Result<Vec<PathBuf>, WikiError> {
    let id = id.trim();
    let asset_dir = vault_root.join("raw/assets");
    if !asset_dir.exists() {
        return Ok(Vec::new());
    }
    let mut paths = Vec::new();
    for entry in fs::read_dir(&asset_dir).map_err(|error| WikiError::Io {
        action: "read raw source assets",
        path: Some(asset_dir.clone()),
        source: error,
    })? {
        let entry = entry.map_err(|error| WikiError::Io {
            action: "read raw source asset entry",
            path: Some(asset_dir.clone()),
            source: error,
        })?;
        if !entry
            .file_type()
            .map_err(|error| WikiError::Io {
                action: "read raw source asset entry type",
                path: Some(entry.path()),
                source: error,
            })?
            .is_file()
        {
            continue;
        }
        let file_name = entry.file_name();
        if file_name.to_str().is_some_and(|name| {
            let path = Path::new(name);
            path.file_stem().and_then(|stem| stem.to_str()) == Some(id)
                && path
                    .extension()
                    .and_then(|extension| extension.to_str())
                    .is_some_and(|extension| !extension.is_empty())
        }) {
            paths.push(Path::new("raw/assets").join(file_name));
        }
    }
    Ok(paths)
}

/// All vault-relative files belonging to `record`: its raw capture, derived
/// digest page, and stored assets. Supersede-style cleanup must remove every
/// entry, or replacing a record leaves an orphan digest behind (#17651).
pub(crate) fn source_record_paths(
    vault_root: &Path,
    record: &SourceRecord,
) -> Result<Vec<PathBuf>, WikiError> {
    let mut paths = vec![raw_source_path(&record.id)?, derived_markdown_path(record)?];
    paths.extend(source_asset_paths_for_id(vault_root, &record.id)?);
    Ok(paths)
}

/// Remove a vault-relative file, tolerating a file that is already gone.
/// Returns whether a file was removed.
pub(crate) fn remove_relative_file(
    vault_root: &Path,
    relative_path: &Path,
) -> Result<bool, WikiError> {
    let relative_path = safe_vault_relative_path(relative_path)?;
    let full_path = vault_root.join(relative_path);
    match fs::remove_file(&full_path) {
        Ok(()) => Ok(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(WikiError::Io {
            action: "remove superseded raw source path",
            path: Some(full_path),
            source: error,
        }),
    }
}

fn safe_vault_relative_path(relative_path: &Path) -> Result<PathBuf, WikiError> {
    if relative_path.as_os_str().is_empty() {
        return Err(WikiError::InvalidInput {
            field: "relative_path",
            message: "path must be a non-empty vault-relative path".to_string(),
        });
    }

    let mut normalized = PathBuf::new();
    for component in relative_path.components() {
        match component {
            Component::Normal(part) => normalized.push(part),
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                return Err(WikiError::InvalidInput {
                    field: "relative_path",
                    message: format!(
                        "path `{}` must stay inside the vault",
                        relative_path.display()
                    ),
                });
            }
        }
    }

    if normalized.as_os_str().is_empty() {
        return Err(WikiError::InvalidInput {
            field: "relative_path",
            message: "path must include a file name".to_string(),
        });
    }

    Ok(normalized)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sources::{CompileStatus, IngestionMethod, SourceKind};

    #[test]
    fn source_paths_trim_safe_ids() {
        assert_eq!(
            raw_source_path("  src-abc  ").expect("raw path"),
            PathBuf::from("raw/src-abc.md")
        );
    }

    #[test]
    fn source_paths_reject_unsafe_ids() {
        for id in ["", ".", "..", "src/abc", r"src\abc", "src..abc"] {
            let error = raw_source_path(id).expect_err("unsafe source id rejected");
            assert_eq!(error.code(), "invalid_input");
        }
    }

    #[test]
    fn source_paths_reject_agent_instruction_filename_ids() {
        for id in ["claude", "CLAUDE", "agents", "Gemini", "qwen"] {
            let error = raw_source_path(id).expect_err("reserved source id rejected");
            assert_eq!(error.code(), "invalid_input");
        }
        // Generated ids keep their prefixes and stay valid.
        assert_eq!(
            raw_source_path("note-claude-md").expect("prefixed id"),
            PathBuf::from("raw/note-claude-md.md")
        );
    }

    #[test]
    fn derived_markdown_path_rejects_unsafe_source_ids() {
        let mut record = source_record("src/abc");

        let error = derived_markdown_path(&record).expect_err("unsafe derived path rejected");
        assert_eq!(error.code(), "invalid_input");

        record.id = "src-abc".to_string();
        assert_eq!(
            derived_markdown_path(&record).expect("derived path"),
            PathBuf::from("knowledge/sources/src-abc.md")
        );
    }

    #[test]
    fn source_record_paths_cover_raw_derived_and_assets() {
        let temp = tempfile::tempdir().expect("tempdir");
        let asset_dir = temp.path().join("raw/assets");
        fs::create_dir_all(&asset_dir).expect("asset dir");
        fs::write(asset_dir.join("src-abc.png"), "asset").expect("write asset");
        fs::create_dir(asset_dir.join("src-abc.pdf")).expect("asset-like directory");
        let record = source_record("src-abc");

        let paths = source_record_paths(temp.path(), &record).expect("record paths");

        assert_eq!(
            paths,
            vec![
                PathBuf::from("raw/src-abc.md"),
                PathBuf::from("knowledge/sources/src-abc.md"),
                PathBuf::from("raw/assets/src-abc.png"),
            ]
        );
    }

    fn source_record(id: &str) -> SourceRecord {
        SourceRecord {
            id: id.to_string(),
            location: "https://example.test/source".to_string(),
            canonical_location: "https://example.test/source".to_string(),
            kind: SourceKind::Url,
            fetched_at: "2026-01-01T00:00:00Z".to_string(),
            last_verified_at: "2026-01-01T00:00:00Z".to_string(),
            fetch_provenance: crate::sources::FetchProvenance::Stub,
            content_hash: "hash".to_string(),
            title: None,
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
            replay: None,
        }
    }
}

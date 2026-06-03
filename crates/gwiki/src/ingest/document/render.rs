use std::fs::File;
use std::io::Write;

use tempfile::{Builder, NamedTempFile};

use crate::document::{DocumentDegradationMatrix, DocumentFailureMode, DocumentUnitCount};
use crate::ingest::{markdown_metadata, path_to_string, single_line};

use super::*;

pub(crate) fn render_raw_document_markdown(
    snapshot: &DocumentSnapshot,
    source_hash: &str,
    asset_path: &Path,
) -> String {
    let asset_path = path_to_string(asset_path);
    let fields = vec![
        ("source_kind", snapshot.kind.to_string()),
        ("source_location", snapshot.location.clone()),
        ("fetched_at", snapshot.fetched_at.clone()),
        ("source_hash", source_hash.to_string()),
        ("source_asset", asset_path.clone()),
    ];

    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(&markdown_title(&snapshot.file_name));
    markdown.push_str("\n\n");
    markdown.push_str("Original document stored under `");
    markdown.push_str(&asset_path);
    markdown.push_str("`.\n");
    markdown
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn write_document_derived_markdown(
    vault_root: &Path,
    scope: &ScopeIdentity,
    record: &crate::sources::SourceRecord,
    snapshot: &DocumentSnapshot,
    title: &str,
    asset_path: &Path,
    extraction: Option<&DocumentExtraction>,
    degradation: Option<&DocumentDegradation>,
) -> Result<PathBuf, WikiError> {
    let relative_path = derived_markdown_path(record);
    let path = vault_root.join(&relative_path);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create document derived markdown directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }

    let markdown = render_document_derived_markdown(
        scope,
        record,
        snapshot,
        title,
        asset_path,
        extraction,
        degradation,
    );
    write_document_markdown_atomic(&path, markdown.as_bytes())?;
    Ok(relative_path)
}

fn write_document_markdown_atomic(path: &Path, contents: &[u8]) -> Result<(), WikiError> {
    let mut temp_file = create_document_temp_file(path)?;
    if let Err(error) = temp_file.write_all(contents) {
        return Err(WikiError::Io {
            action: "write document derived markdown temp file",
            path: Some(temp_file.path().to_path_buf()),
            source: error,
        });
    }
    if let Err(error) = temp_file.as_file().sync_all() {
        return Err(WikiError::Io {
            action: "sync document derived markdown temp file",
            path: Some(temp_file.path().to_path_buf()),
            source: error,
        });
    }
    if let Err(error) = temp_file.persist(path) {
        return Err(WikiError::Io {
            action: "write document derived markdown",
            path: Some(path.to_path_buf()),
            source: error.error,
        });
    }
    sync_parent_dir(path)
}

fn create_document_temp_file(path: &Path) -> Result<NamedTempFile, WikiError> {
    let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    else {
        return Err(WikiError::Io {
            action: "create document derived markdown temp file",
            path: Some(path.to_path_buf()),
            source: std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "document derived markdown target has no parent directory",
            ),
        });
    };
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("document.md");
    Builder::new()
        .prefix(&format!(".{file_name}."))
        .suffix(".tmp")
        .tempfile_in(parent)
        .map_err(|source| WikiError::Io {
            action: "create document derived markdown temp file",
            path: Some(parent.to_path_buf()),
            source,
        })
}

fn sync_parent_dir(path: &Path) -> Result<(), WikiError> {
    #[cfg(not(unix))]
    {
        let _ = path;
        Ok(())
    }
    #[cfg(unix)]
    {
        let Some(parent) = path.parent() else {
            return Ok(());
        };
        File::open(parent)
            .and_then(|dir| dir.sync_all())
            .map_err(|error| WikiError::Io {
                action: "sync document derived markdown directory",
                path: Some(parent.to_path_buf()),
                source: error,
            })
    }
}

fn render_document_derived_markdown(
    scope: &ScopeIdentity,
    record: &crate::sources::SourceRecord,
    snapshot: &DocumentSnapshot,
    title: &str,
    asset_path: &Path,
    extraction: Option<&DocumentExtraction>,
    degradation: Option<&DocumentDegradation>,
) -> String {
    let asset_path = path_to_string(asset_path);
    let raw_path = format!("raw/{}.md", record.id);
    let mut fields = vec![
        ("title".to_string(), title.to_string()),
        ("source_kind".to_string(), snapshot.kind.to_string()),
        ("source_location".to_string(), record.location.clone()),
        ("source_hash".to_string(), record.content_hash.clone()),
        ("source_asset".to_string(), asset_path.clone()),
        ("source_raw".to_string(), raw_path.clone()),
        ("fetched_at".to_string(), record.fetched_at.clone()),
        ("scope_kind".to_string(), scope.kind.as_str().to_string()),
        ("scope_id".to_string(), scope.id.clone()),
        (
            "document_status".to_string(),
            if extraction.is_some() {
                "extracted".to_string()
            } else {
                "unavailable".to_string()
            },
        ),
    ];
    if let Some(extraction) = extraction {
        fields.push((
            extraction.units_label.to_string(),
            extraction.units_count.to_string(),
        ));
    }
    if let Some(degradation) = degradation {
        fields.extend(DocumentDegradationMatrix::metadata(
            degradation,
            snapshot.bytes.len(),
        ));
    } else {
        fields.push((
            "file_size_bytes".to_string(),
            snapshot.bytes.len().to_string(),
        ));
    }

    let mut markdown = {
        let field_refs = fields
            .iter()
            .map(|(key, value)| (key.as_str(), value.clone()))
            .collect::<Vec<_>>();
        markdown_metadata(&field_refs)
    };
    markdown.push_str("# ");
    markdown.push_str(title);
    markdown.push_str("\n\n");
    markdown.push_str("Original document: `");
    markdown.push_str(&asset_path);
    markdown.push_str("`\n\n");
    markdown.push_str("Raw source: `");
    markdown.push_str(&raw_path);
    markdown.push_str("`\n\n");

    if let Some(extraction) = extraction {
        markdown.push_str(&extraction.markdown);
        if !markdown.ends_with('\n') {
            markdown.push('\n');
        }
    } else if let Some(degradation) = degradation {
        markdown.push_str(&DocumentDegradationMatrix::markdown_section(degradation));
    }

    markdown.push_str("## Source References\n\n");
    markdown.push_str("- Raw source: `");
    markdown.push_str(&raw_path);
    markdown.push_str("`\n");
    markdown.push_str("- Original document: `");
    markdown.push_str(&asset_path);
    markdown.push_str("`\n");
    if let Some(citation) = &record.citation {
        markdown.push_str("- Citation: ");
        markdown.push_str(&single_line(citation));
        markdown.push('\n');
    }
    markdown
}

fn derived_markdown_path(record: &crate::sources::SourceRecord) -> PathBuf {
    PathBuf::from("wiki")
        .join("sources")
        .join(format!("{}.md", record.id))
}

pub(crate) fn document_degradation_for_error(
    request: &DocumentRequest<'_>,
    error: String,
) -> DocumentDegradation {
    let mode = match request.kind {
        SourceKind::Html => DocumentFailureMode::HtmlParseError,
        SourceKind::Office => DocumentFailureMode::OfficeParseError,
        _ => DocumentFailureMode::OfficeParseError,
    };
    DocumentDegradation::new(
        mode,
        document_unit_count_for_failure(request.file_name, request.kind),
        format!("Document parsing failed: {error}; original asset is preserved."),
    )
}

fn document_unit_count_for_failure(file_name: &str, kind: &SourceKind) -> DocumentUnitCount {
    match kind {
        SourceKind::Html => DocumentUnitCount::pages(1),
        SourceKind::Office => match extension(file_name).as_deref() {
            Some("pptx") => DocumentUnitCount::slides(0),
            Some("xlsx" | "xls" | "ods") => DocumentUnitCount::sheets(0),
            _ => DocumentUnitCount::pages(0),
        },
        _ => DocumentUnitCount::pages(0),
    }
}

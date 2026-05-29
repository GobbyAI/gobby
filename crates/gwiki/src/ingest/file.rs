use std::path::Path;

use crate::WikiError;
use crate::ingest::{
    IngestResult, index_after_ingest, markdown_metadata, markdown_title, text_from_utf8_lossy,
    write_asset, write_raw_markdown,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StdinSnapshot {
    pub label: String,
    pub fetched_at: String,
    pub bytes: Vec<u8>,
}

pub fn ingest_path(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    path: &Path,
    fetched_at: &str,
) -> Result<IngestResult, WikiError> {
    let bytes = std::fs::read(path).map_err(|error| WikiError::Io {
        action: "read source file",
        path: Some(path.to_path_buf()),
        source: error.to_string(),
    })?;
    let kind = detect_source_kind(path);
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("source");
    let title = markdown_title(file_name);
    let location = path.to_string_lossy().replace('\\', "/");
    let draft = SourceDraft {
        location: location.clone(),
        kind: kind.clone(),
        fetched_at: fetched_at.to_string(),
        content: bytes.clone(),
        title: Some(title.clone()),
        citation: Some(location.clone()),
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
    };
    let record = SourceManifest::register(vault_root, draft)?;
    let asset_path = should_store_asset(&kind)
        .then(|| write_asset(vault_root, &record, file_name, &bytes))
        .transpose()?;
    let markdown = render_file_markdown(
        &title,
        &location,
        fetched_at,
        &record.content_hash,
        &kind,
        &bytes,
        asset_path.as_deref(),
    );
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;
    index_after_ingest(vault_root, store)?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path,
    })
}

pub fn ingest_stdin(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    snapshot: StdinSnapshot,
) -> Result<IngestResult, WikiError> {
    let title = markdown_title(&snapshot.label);
    let location = format!("stdin:{}", snapshot.label);
    let draft = SourceDraft {
        location: location.clone(),
        kind: SourceKind::Stdin,
        fetched_at: snapshot.fetched_at.clone(),
        content: snapshot.bytes.clone(),
        title: Some(title.clone()),
        citation: None,
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
    };
    let record = SourceManifest::register(vault_root, draft)?;
    let markdown = render_file_markdown(
        &title,
        &location,
        &snapshot.fetched_at,
        &record.content_hash,
        &SourceKind::Stdin,
        &snapshot.bytes,
        None,
    );
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;
    index_after_ingest(vault_root, store)?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path: None,
    })
}

fn detect_source_kind(path: &Path) -> SourceKind {
    match path
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("pdf") => SourceKind::Pdf,
        Some("mp4" | "mov" | "m4v" | "webm" | "mkv") => SourceKind::Video,
        Some("md" | "markdown") => SourceKind::Markdown,
        Some("txt" | "text") => SourceKind::Text,
        _ => SourceKind::File,
    }
}

fn should_store_asset(kind: &SourceKind) -> bool {
    matches!(kind, SourceKind::Pdf | SourceKind::Video | SourceKind::File)
}

fn render_file_markdown(
    title: &str,
    location: &str,
    fetched_at: &str,
    source_hash: &str,
    kind: &SourceKind,
    bytes: &[u8],
    asset_path: Option<&Path>,
) -> String {
    let mut fields = vec![
        ("source_kind", kind.to_string()),
        ("source_location", location.to_string()),
        ("fetched_at", fetched_at.to_string()),
        ("source_hash", source_hash.to_string()),
    ];
    if let Some(asset_path) = asset_path {
        fields.push(("source_asset", asset_path.display().to_string()));
    }

    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(title);
    markdown.push_str("\n\n");

    match kind {
        SourceKind::Markdown | SourceKind::Text | SourceKind::Stdin => {
            markdown.push_str(&text_from_utf8_lossy(bytes));
            if !markdown.ends_with('\n') {
                markdown.push('\n');
            }
        }
        _ => {
            markdown.push_str("Original artifact stored under `");
            if let Some(asset_path) = asset_path {
                markdown.push_str(&asset_path.display().to_string());
            } else {
                markdown.push_str("raw/assets/");
            }
            markdown.push_str("`.\n");
        }
    }

    markdown
}

#[cfg(test)]
mod tests {
    use gobby_core::indexing::content_hash;

    use super::*;
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::MemoryWikiStore;

    #[test]
    fn file_and_stdin_ingest_hash_sources() {
        let temp = tempfile::tempdir().expect("tempdir");
        let file_bytes = b"# Field Notes\n\nLocal markdown source.\n";
        let file_path = temp.path().join("field-notes.md");
        std::fs::write(&file_path, file_bytes).expect("write local file");
        let stdin_bytes = b"stdin source text\n".to_vec();
        let mut store = MemoryWikiStore::default();

        let file_result = ingest_path(temp.path(), &mut store, &file_path, "2026-05-29T16:45:00Z")
            .expect("ingest local file");
        let stdin_result = ingest_stdin(
            temp.path(),
            &mut store,
            StdinSnapshot {
                label: "gwiki-stdin".to_string(),
                fetched_at: "2026-05-29T16:46:00Z".to_string(),
                bytes: stdin_bytes.clone(),
            },
        )
        .expect("ingest stdin");

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 2);
        let markdown_entry = manifest
            .entries
            .iter()
            .find(|entry| entry.kind == SourceKind::Markdown)
            .expect("markdown source kind");
        let stdin_entry = manifest
            .entries
            .iter()
            .find(|entry| entry.kind == SourceKind::Stdin)
            .expect("stdin source kind");
        assert_eq!(markdown_entry.content_hash, content_hash(file_bytes));
        assert_eq!(stdin_entry.content_hash, content_hash(&stdin_bytes));

        let raw_file = std::fs::read_to_string(temp.path().join(file_result.raw_path))
            .expect("file raw markdown");
        assert!(raw_file.contains("# Field Notes"));
        let raw_stdin = std::fs::read_to_string(temp.path().join(stdin_result.raw_path))
            .expect("stdin raw markdown");
        assert!(raw_stdin.contains("stdin source text"));
    }
}

//! Ingestion helpers for immutable raw wiki sources.

pub mod audio;
#[cfg(feature = "documents")]
pub mod document;
pub mod file;
pub mod git;
pub mod image;
mod immutable;
pub mod pdf;
pub mod session;
pub(crate) mod session_archive;
pub mod url;
pub mod video;

use std::path::{Path, PathBuf};

use serde::Serialize;

use immutable::{
    prepare_immutable_path, validate_existing_raw_bytes, write_immutable, write_immutable_file,
};

use crate::WikiError;
use crate::indexer;
use crate::paths::raw_source_path;
use crate::sources::SourceRecord;
#[cfg(feature = "documents")]
pub(crate) use crate::sources::atomic::sync_parent_dir;
use crate::store::WikiIndexStore;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IngestResult {
    pub record: SourceRecord,
    pub raw_path: PathBuf,
    pub asset_path: Option<PathBuf>,
}

pub(crate) fn lowercase_extension(path: impl AsRef<Path>) -> Option<String> {
    path.as_ref()
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase)
}

fn raw_markdown_relative_path(record: &SourceRecord) -> Result<PathBuf, WikiError> {
    raw_source_path(&record.id)
}

/// Return the immutable raw markdown path for `record` when it already exists.
///
/// An unchanged re-ingest dedups to the existing manifest record, but media
/// raw markdown embeds run-varying capture data (fetched_at, transcription and
/// vision output, degradation state), so its original bytes cannot be
/// reproduced. Callers reuse the first capture instead of re-rendering and
/// failing the immutable raw write (#17650).
pub(crate) fn existing_raw_markdown(vault_root: &Path, record: &SourceRecord) -> Option<PathBuf> {
    let raw_path = raw_markdown_relative_path(record).ok()?;
    let root = vault_root.canonicalize().ok()?;
    let path = root.join(&raw_path);
    let metadata = std::fs::symlink_metadata(&path).ok()?;
    // Only a regular file is a reusable capture; anything else blocking the
    // path must fall through to the raw write so it surfaces as an error.
    if !metadata.file_type().is_file() {
        return None;
    }
    path.canonicalize()
        .ok()?
        .starts_with(root)
        .then_some(raw_path)
}

pub(crate) fn write_raw_markdown(
    vault_root: &Path,
    record: &SourceRecord,
    markdown: &str,
) -> Result<PathBuf, WikiError> {
    let raw_path = raw_markdown_relative_path(record)?;
    let normalized = crate::markdown::normalize(markdown);
    let path = prepare_immutable_path(vault_root, &raw_path)?;
    if path.exists() {
        match validate_existing_raw_bytes(&path, &raw_path, normalized.as_bytes()) {
            Ok(()) => return Ok(raw_path),
            Err(error) => {
                if normalized != markdown
                    && validate_existing_raw_bytes(&path, &raw_path, markdown.as_bytes()).is_ok()
                {
                    return Ok(raw_path);
                }
                return Err(error);
            }
        }
    }
    write_immutable(vault_root, &raw_path, normalized.as_bytes())?;
    Ok(raw_path)
}

pub(crate) fn write_asset(
    vault_root: &Path,
    record: &SourceRecord,
    file_name: &str,
    bytes: &[u8],
) -> Result<PathBuf, WikiError> {
    let asset_path = asset_path(record, file_name);
    write_immutable(vault_root, &asset_path, bytes)?;
    Ok(asset_path)
}

pub(crate) fn write_asset_with_suffix(
    vault_root: &Path,
    record: &SourceRecord,
    suffix: &str,
    file_name: &str,
    bytes: &[u8],
) -> Result<PathBuf, WikiError> {
    let extension = sanitized_extension_for_file_name(file_name);
    let suffix = sanitize_asset_suffix(suffix);
    let asset_path = PathBuf::from("raw")
        .join("assets")
        .join(format!("{}.{}.{}", record.id, suffix, extension));
    write_immutable(vault_root, &asset_path, bytes)?;
    Ok(asset_path)
}

pub(crate) fn write_asset_from_path(
    vault_root: &Path,
    record: &SourceRecord,
    file_name: &str,
    source_path: &Path,
    content_hash: &str,
) -> Result<PathBuf, WikiError> {
    let asset_path = asset_path(record, file_name);
    write_immutable_file(vault_root, &asset_path, source_path, content_hash)?;
    Ok(asset_path)
}

fn sanitize_asset_suffix(value: &str) -> String {
    let mut suffix = String::new();
    let mut last_dash = false;
    for ch in value.chars() {
        if ch.is_ascii_alphanumeric() || ch == '_' {
            suffix.push(ch);
            last_dash = false;
        } else if !last_dash && !suffix.is_empty() {
            suffix.push('-');
            last_dash = true;
        }
    }
    while suffix.ends_with('-') {
        suffix.pop();
    }
    if suffix.is_empty() {
        "asset".to_string()
    } else {
        suffix
    }
}

pub(crate) fn index_after_ingest(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    progress: &mut crate::progress::ProgressOptions<'_>,
) -> Result<(), WikiError> {
    let options = crate::support::config::local_index_options()?;
    let mut paths = vec![PathBuf::from("raw/INDEX.md")];
    let source_notes_root = vault_root.join("knowledge/sources");
    if source_notes_root.is_dir() {
        let entries =
            std::fs::read_dir(&source_notes_root).map_err(|error| WikiError::InvalidInput {
                field: "index",
                message: error.to_string(),
            })?;
        let mut source_notes = Vec::new();
        for entry in entries {
            let path = entry
                .map_err(|error| WikiError::InvalidInput {
                    field: "index",
                    message: error.to_string(),
                })?
                .path();
            if path.is_file() && path.extension().is_some_and(|ext| ext == "md") {
                let relative_path =
                    path.strip_prefix(vault_root)
                        .map_err(|error| WikiError::InvalidInput {
                            field: "index",
                            message: error.to_string(),
                        })?;
                source_notes.push(relative_path.to_path_buf());
            }
        }
        source_notes.sort();
        paths.extend(source_notes);
    }
    for path in paths {
        indexer::index_path(vault_root, store, path, options, progress).map_err(|error| {
            WikiError::InvalidInput {
                field: "index",
                message: error.to_string(),
            }
        })?;
    }
    Ok(())
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub(crate) fn write_raw_then_index(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    record: SourceRecord,
    markdown: &str,
    asset_path: Option<PathBuf>,
) -> Result<IngestResult, WikiError> {
    // Reuse the first capture on unchanged re-ingest: register() dedups to
    // the existing record and the immutable raw write only tolerates
    // byte-identical bytes, so a drifted re-render must not reach it (#17653).
    let raw_path = match existing_raw_markdown(vault_root, &record) {
        Some(existing) => existing,
        None => write_raw_markdown(vault_root, &record, markdown)?,
    };
    index_after_ingest(
        vault_root,
        store,
        &mut crate::progress::ProgressOptions::default(),
    )?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path,
    })
}

pub(crate) fn markdown_metadata(fields: &[(&str, String)]) -> String {
    let fields = fields
        .iter()
        .map(|(key, value)| (*key, MetadataValue::string(value.clone())))
        .collect::<Vec<_>>();
    markdown_metadata_values(&fields)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum MetadataValue {
    String(String),
    Number(String),
    Bool(bool),
    Json(String),
}

impl MetadataValue {
    pub(crate) fn string(value: impl Into<String>) -> Self {
        Self::String(value.into())
    }

    pub(crate) fn number(value: impl ToString) -> Self {
        Self::Number(value.to_string())
    }

    pub(crate) fn bool(value: bool) -> Self {
        Self::Bool(value)
    }

    pub(crate) fn json(value: impl Serialize) -> Self {
        Self::Json(serde_json::to_string(&value).unwrap_or_else(|_| "null".to_string()))
    }
}

pub(crate) fn markdown_metadata_values(fields: &[(&str, MetadataValue)]) -> String {
    let mut metadata = String::from("---\n");
    for (key, value) in fields {
        metadata.push_str(key);
        metadata.push_str(": ");
        metadata.push_str(&yaml_metadata_value(key, value));
        metadata.push('\n');
    }
    metadata.push_str("---\n\n");
    metadata
}

fn yaml_metadata_value(key: &str, value: &MetadataValue) -> String {
    match value {
        MetadataValue::String(value) => yaml_metadata_scalar(key, value),
        MetadataValue::Number(value) => yaml_numeric_scalar(value),
        MetadataValue::Bool(value) => value.to_string(),
        MetadataValue::Json(value) => yaml_json_value(value),
    }
}

fn yaml_metadata_scalar(key: &str, value: &str) -> String {
    if key == "content_type" {
        let value = single_line(value);
        return quote_yaml_string(&value);
    }
    yaml_safe_single_line_scalar(value)
}

fn yaml_safe_single_line_scalar(value: &str) -> String {
    let value = single_line(value);
    if yaml_plain_scalar_is_safe(&value) {
        value
    } else {
        quote_yaml_string(&value)
    }
}

fn yaml_numeric_scalar(value: &str) -> String {
    let value = single_line(value);
    if yaml_numeric_scalar_is_safe(&value) {
        value
    } else {
        quote_yaml_string(&value)
    }
}

fn yaml_json_value(value: &str) -> String {
    let value = single_line(value);
    if serde_json::from_str::<serde_json::Value>(&value).is_ok() {
        value
    } else {
        quote_yaml_string(&value)
    }
}

fn yaml_plain_scalar_is_safe(value: &str) -> bool {
    if value.is_empty() {
        return false;
    }
    let lower = value.to_ascii_lowercase();
    if matches!(
        lower.as_str(),
        "true" | "false" | "null" | "~" | ".nan" | ".inf" | "+.inf" | "-.inf"
    ) || value.parse::<i64>().is_ok()
        || value.parse::<f64>().is_ok()
        || yaml_plain_scalar_is_timestamp(value)
    {
        return false;
    }
    !value.contains(':')
        && !value.contains(" #")
        && !value.contains(['"', '\''])
        && !value.starts_with([
            '-', '?', ':', '#', '{', '}', '[', ']', ',', '&', '*', '!', '|', '>', '%', '@', '`',
        ])
}

fn yaml_numeric_scalar_is_safe(value: &str) -> bool {
    value.parse::<i64>().is_ok() || value.parse::<f64>().is_ok_and(|number| number.is_finite())
}

fn yaml_plain_scalar_is_timestamp(value: &str) -> bool {
    let bytes = value.as_bytes();
    has_yaml_date_prefix(bytes) && matches!(bytes.get(10), None | Some(b'T' | b't' | b' '))
}

fn has_yaml_date_prefix(bytes: &[u8]) -> bool {
    bytes.len() >= 10
        && bytes[0..4].iter().all(u8::is_ascii_digit)
        && bytes[4] == b'-'
        && bytes[5..7].iter().all(u8::is_ascii_digit)
        && bytes[7] == b'-'
        && bytes[8..10].iter().all(u8::is_ascii_digit)
}

fn quote_yaml_string(value: &str) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| "\"\"".to_string())
}

pub(crate) fn single_line(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub(crate) fn markdown_title(value: &str) -> String {
    single_line(value).trim_matches('#').trim().to_string()
}

pub(crate) fn text_from_utf8_lossy(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).replace("\r\n", "\n")
}

pub(crate) fn path_to_string(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

/// Syncs the containing directory on Unix so the atomic rename is durable.
/// Non-Unix platforms keep the file sync but skip directory `sync_all`.
pub(crate) fn asset_path(record: &SourceRecord, file_name: &str) -> PathBuf {
    let extension = sanitized_extension_for_file_name(file_name);
    PathBuf::from("raw")
        .join("assets")
        .join(format!("{}.{}", record.id, extension))
}

fn sanitized_extension_for_file_name(file_name: &str) -> String {
    // Path-shaped names from external inputs are reduced to their basename
    // before extension extraction, so directory components cannot affect the
    // raw asset path.
    let basename = Path::new(file_name)
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or(file_name);
    Path::new(basename)
        .extension()
        .and_then(|value| value.to_str())
        .map(sanitize_extension)
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "bin".to_string())
}

fn sanitize_extension(extension: &str) -> String {
    extension
        .chars()
        .filter(|ch| ch.is_ascii_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;
    use std::path::{Path, PathBuf};

    use super::{
        MetadataValue, asset_path, existing_raw_markdown, markdown_metadata,
        markdown_metadata_values, write_asset_from_path, write_raw_markdown,
    };
    use gobby_core::ai_context::AiContext;
    use gobby_core::config::EnvOnlySource;
    use gobby_core::indexing::content_hash;

    use crate::ScopeIdentity;
    use crate::api::IngestFileOptions;
    use crate::ingest::file;
    use crate::sources::{CompileStatus, IngestionMethod, SourceKind, SourceRecord};
    use crate::store::{FakeWikiStore, WikiIngestionEvent};

    fn no_ai_context() -> AiContext {
        let mut source = EnvOnlySource;
        let mut context = AiContext::resolve(None, &mut source);
        IngestFileOptions {
            no_ai: true,
            ..IngestFileOptions::default()
        }
        .apply_to_ai_context(&mut context);
        context
    }

    fn write_file(root: &std::path::Path, relative: &str, contents: &str) {
        let path = root.join(relative);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("create parent");
        }
        std::fs::write(path, contents).expect("write file");
    }

    fn test_source_record() -> SourceRecord {
        SourceRecord {
            id: "source-1".to_string(),
            location: "/tmp/report.pdf".to_string(),
            canonical_location: "/tmp/report.pdf".to_string(),
            kind: SourceKind::Pdf,
            fetched_at: "2026-05-29T17:00:00Z".to_string(),
            last_verified_at: "2026-05-29T17:00:00Z".to_string(),
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

    #[test]
    fn asset_path_uses_basename_before_extension_extraction() {
        let record = test_source_record();

        assert_eq!(
            asset_path(&record, "../nested/report.PDF"),
            PathBuf::from("raw/assets/source-1.pdf")
        );
        assert_eq!(
            asset_path(&record, "../nested/no-extension"),
            PathBuf::from("raw/assets/source-1.bin")
        );
    }

    #[test]
    fn markdown_metadata_quotes_yaml_sensitive_scalars() {
        let metadata = markdown_metadata(&[
            ("source_kind", "pdf".to_string()),
            ("source_location", "https://example.com/a: b #c".to_string()),
            ("compact_colon", "key:value".to_string()),
            ("comment", "# heading".to_string()),
            ("draft", "true".to_string()),
            ("empty", "~".to_string()),
            ("revision", "1.20".to_string()),
            ("date", "2026-06-05".to_string()),
            ("timestamp", "2026-06-05T10:11:12Z".to_string()),
            ("timestamp_space", "2026-06-05 10:11:12".to_string()),
            ("content_type", "text/html".to_string()),
        ]);

        assert!(metadata.contains("source_kind: pdf\n"));
        assert!(metadata.contains("source_location: \"https://example.com/a: b #c\"\n"));
        assert!(metadata.contains("compact_colon: \"key:value\"\n"));
        assert!(metadata.contains("comment: \"# heading\"\n"));
        assert!(metadata.contains("draft: \"true\"\n"));
        assert!(metadata.contains("empty: \"~\"\n"));
        assert!(metadata.contains("revision: \"1.20\"\n"));
        assert!(metadata.contains("date: \"2026-06-05\"\n"));
        assert!(metadata.contains("timestamp: \"2026-06-05T10:11:12Z\"\n"));
        assert!(metadata.contains("timestamp_space: \"2026-06-05 10:11:12\"\n"));
        assert!(metadata.contains("content_type: \"text/html\"\n"));
    }

    #[test]
    fn markdown_metadata_allows_explicit_typed_values() {
        let counts = BTreeMap::from([("Read".to_string(), 2_u64), ("Write".to_string(), 1)]);
        let metadata = markdown_metadata_values(&[
            ("file_size_bytes", MetadataValue::number(42)),
            ("duration_seconds", MetadataValue::number(13)),
            ("unsafe_number", MetadataValue::Number("NaN".to_string())),
            ("is_subagent", MetadataValue::bool(true)),
            ("tool_counts", MetadataValue::json(&counts)),
        ]);

        assert!(metadata.contains("file_size_bytes: 42\n"));
        assert!(metadata.contains("duration_seconds: 13\n"));
        assert!(metadata.contains("unsafe_number: \"NaN\"\n"));
        assert!(metadata.contains("is_subagent: true\n"));
        assert!(metadata.contains("tool_counts: {\"Read\":2,\"Write\":1}\n"));
    }

    #[test]
    fn generated_raw_markdown_is_normalized_before_write() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = test_source_record();
        let raw_path = write_raw_markdown(
            temp.path(),
            &record,
            "---\nsource_kind: pdf\n---\n\n\n# Report\n\n\n```text\none\n\n\ntwo\n```\n\n\nBody\n",
        )
        .expect("write raw markdown");
        let raw = std::fs::read_to_string(temp.path().join(raw_path)).expect("read raw");

        assert!(!raw.contains("\n\n\n# Report"));
        assert!(!raw.contains("```\n\n\nBody"));
        assert!(raw.contains("```text\none\n\n\ntwo\n```"));
        assert_eq!(raw, crate::markdown::normalize(&raw));
    }

    #[test]
    fn raw_markdown_rejects_unsafe_source_id() {
        let temp = tempfile::tempdir().expect("tempdir");
        let mut record = test_source_record();
        record.id = "../outside".to_string();

        let error = write_raw_markdown(temp.path(), &record, "# Source\n")
            .expect_err("unsafe source id must be rejected");

        assert_eq!(error.code(), "invalid_input");
        assert!(!temp.path().join("raw").exists());
    }

    #[cfg(unix)]
    #[test]
    fn raw_markdown_rejects_symlink_destination() {
        use std::os::unix::fs::symlink;

        let temp = tempfile::tempdir().expect("tempdir");
        let vault = temp.path().join("vault");
        let outside = temp.path().join("outside.md");
        std::fs::create_dir_all(vault.join("raw")).expect("raw dir");
        std::fs::write(&outside, "# Source\n").expect("outside note");
        let record = test_source_record();
        symlink(&outside, vault.join("raw/source-1.md")).expect("source symlink");

        assert_eq!(existing_raw_markdown(&vault, &record), None);
        let error = write_raw_markdown(&vault, &record, "# Source\n")
            .expect_err("source-note symlink must be rejected");

        assert_eq!(error.code(), "invalid_input");
        assert_eq!(
            std::fs::read_to_string(&outside).expect("outside note remains"),
            "# Source\n"
        );
    }

    #[cfg(unix)]
    #[test]
    fn raw_markdown_rejects_parent_symlink_outside_vault() {
        use std::os::unix::fs::symlink;

        let temp = tempfile::tempdir().expect("tempdir");
        let vault = temp.path().join("vault");
        let outside = temp.path().join("outside");
        std::fs::create_dir(&vault).expect("vault");
        std::fs::create_dir(&outside).expect("outside");
        symlink(&outside, vault.join("raw")).expect("raw symlink");
        let record = test_source_record();

        let error = write_raw_markdown(&vault, &record, "# Source\n")
            .expect_err("vault-escaping parent symlink must be rejected");

        assert_eq!(error.code(), "invalid_input");
        assert!(!outside.join("source-1.md").exists());
    }

    #[test]
    fn immutable_file_requires_declared_source_hash_before_copy() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = test_source_record();
        let source = temp.path().join("source.txt");
        std::fs::write(&source, "source bytes").expect("write source");

        let error = write_asset_from_path(
            temp.path(),
            &record,
            "source.txt",
            &source,
            "intentionally-stale-hash",
        )
        .expect_err("stale hash rejected before copy");

        assert_eq!(error.code(), "invalid_input");
        assert!(!temp.path().join("raw/assets/source-1.txt").exists());
    }

    #[test]
    fn immutable_file_existing_match_requires_declared_hash() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = test_source_record();
        let first_source = temp.path().join("first.txt");
        let second_source = temp.path().join("second.txt");
        let different_source = temp.path().join("different.txt");
        std::fs::write(&first_source, "same bytes").expect("write first");
        std::fs::write(&second_source, "same bytes").expect("write second");
        std::fs::write(&different_source, "different bytes").expect("write different");
        let same_hash = gobby_core::indexing::file_content_hash(&first_source).expect("hash first");
        let different_hash =
            gobby_core::indexing::file_content_hash(&different_source).expect("hash different");

        let asset_path = write_asset_from_path(
            temp.path(),
            &record,
            "source.txt",
            &first_source,
            &same_hash,
        )
        .expect("first asset write");
        write_asset_from_path(
            temp.path(),
            &record,
            "source.txt",
            &second_source,
            &same_hash,
        )
        .expect("matching existing asset is idempotent");
        let stale_error = write_asset_from_path(
            temp.path(),
            &record,
            "source.txt",
            &second_source,
            "another-stale-hash",
        )
        .expect_err("matching existing asset still requires declared hash");
        let error = write_asset_from_path(
            temp.path(),
            &record,
            "source.txt",
            &different_source,
            &different_hash,
        )
        .expect_err("different source rejected");

        assert_eq!(asset_path, PathBuf::from("raw/assets/source-1.txt"));
        assert_eq!(stale_error.code(), "invalid_input");
        assert_eq!(error.code(), "invalid_input");
    }

    #[test]
    fn ingest_indexes_raw_without_wiki_rewrite() {
        let temp = tempfile::tempdir().expect("tempdir");
        let wiki_body = "# Existing Topic\n\nUser-authored notes stay intact.\n";
        write_file(temp.path(), "knowledge/topics/existing.md", wiki_body);
        let source_path = temp.path().join("capture.txt");
        std::fs::write(&source_path, "captured source text\n").expect("write source");

        let mut store = FakeWikiStore::default();
        store.file_hashes.insert(
            PathBuf::from("knowledge/topics/existing.md"),
            content_hash(wiki_body.as_bytes()),
        );
        let before = std::fs::read_to_string(temp.path().join("knowledge/topics/existing.md"))
            .expect("read existing before");
        let scope = ScopeIdentity::global();
        let ai_context = no_ai_context();
        let options = IngestFileOptions {
            no_ai: true,
            ..IngestFileOptions::default()
        };

        let result = file::ingest_path(
            temp.path(),
            &mut store,
            &scope,
            &ai_context,
            &options,
            file::LocalFileSnapshot {
                path: &source_path,
                fetched_at: "2026-05-29T17:00:00Z",
            },
            &mut crate::progress::ProgressOptions::default(),
        )
        .expect("ingest path");

        let after = std::fs::read_to_string(temp.path().join("knowledge/topics/existing.md"))
            .expect("read existing after");
        assert_eq!(after, before);
        assert!(temp.path().join(&result.raw_path).is_file());
        assert!(store.documents.contains_key(&PathBuf::from("raw/INDEX.md")));
        assert!(
            !store
                .documents
                .contains_key(&PathBuf::from("knowledge/topics/existing.md"))
        );
        assert!(store.ingestions.iter().any(|ingestion| {
            ingestion.path == Path::new("raw/INDEX.md")
                && ingestion.event == WikiIngestionEvent::Added
        }));
        assert!(
            store
                .ingestions
                .iter()
                .all(|ingestion| ingestion.path == Path::new("raw/INDEX.md")),
            "interactive ingest must not reconcile unrelated vault pages"
        );
    }
}

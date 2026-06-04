use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::WikiError;
use crate::ingest::{
    asset_path as source_asset_path, index_after_ingest, markdown_metadata, markdown_title,
    text_from_utf8_lossy, write_asset, write_raw_markdown,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;
use crate::support::env::max_inbox_item_bytes_from_env;

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct CollectReport {
    pub accepted: Vec<CollectAction>,
    pub skipped: Vec<CollectAction>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CollectAction {
    pub inbox_path: String,
    pub status: CollectStatus,
    pub kind: Option<String>,
    pub raw_path: Option<String>,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CollectStatus {
    Accepted,
    Skipped,
}

enum InboxKind {
    Url(String),
    File(SourceKind),
}

pub fn collect_inbox(vault_root: &Path, fetched_at: &str) -> Result<CollectReport, WikiError> {
    collect_inbox_with_limit(vault_root, fetched_at, max_inbox_item_bytes_from_env())
}

fn collect_inbox_with_limit(
    vault_root: &Path,
    fetched_at: &str,
    max_item_bytes: u64,
) -> Result<CollectReport, WikiError> {
    ensure_collect_paths(vault_root)?;

    let inbox = vault_root.join("inbox");
    let mut entries = fs::read_dir(&inbox)
        .map_err(|error| io_error("read inbox", &inbox, error))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| io_error("read inbox entry", &inbox, error))?;
    entries.sort_by_key(|entry| entry.file_name());

    let mut report = CollectReport::default();
    for entry in entries {
        let path = entry.path();
        if is_status_sidecar(&path) {
            continue;
        }

        let relative = relative_to_vault(vault_root, &path);
        let file_type = entry
            .file_type()
            .map_err(|error| io_error("read inbox file type", &path, error))?;
        if file_type.is_dir() {
            skip_item(
                vault_root,
                fetched_at,
                relative,
                path,
                "directories are ambiguous inbox items",
                &mut report,
            )?;
            continue;
        }
        if !file_type.is_file() {
            skip_item(
                vault_root,
                fetched_at,
                relative,
                path,
                "unsupported inbox item type",
                &mut report,
            )?;
            continue;
        }

        let metadata = entry
            .metadata()
            .map_err(|error| io_error("read inbox item metadata", &path, error))?;
        if metadata.len() > max_item_bytes {
            skip_item(
                vault_root,
                fetched_at,
                relative,
                path,
                format!("inbox item exceeds {max_item_bytes} byte limit"),
                &mut report,
            )?;
            continue;
        }

        let Some(bytes) = read_inbox_item_limited(&path, max_item_bytes)? else {
            skip_item(
                vault_root,
                fetched_at,
                relative,
                path,
                format!("inbox item exceeds {max_item_bytes} byte limit"),
                &mut report,
            )?;
            continue;
        };
        match classify_inbox_item(&path, &bytes) {
            Ok(kind) => {
                accept_item(
                    vault_root,
                    fetched_at,
                    relative,
                    path,
                    bytes,
                    kind,
                    &mut report,
                )?;
            }
            Err(reason) => skip_item(vault_root, fetched_at, relative, path, reason, &mut report)?,
        }
    }

    append_log(vault_root, fetched_at, &report)?;
    Ok(report)
}

pub fn collect_inbox_and_index(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    fetched_at: &str,
) -> Result<CollectReport, WikiError> {
    let report = collect_inbox(vault_root, fetched_at)?;
    if !report.accepted.is_empty() {
        index_after_ingest(vault_root, store)?;
    }
    Ok(report)
}

fn read_inbox_item_limited(path: &Path, max_item_bytes: u64) -> Result<Option<Vec<u8>>, WikiError> {
    let file = fs::File::open(path).map_err(|error| io_error("read inbox item", path, error))?;
    let mut bytes = Vec::new();
    file.take(max_item_bytes.saturating_add(1))
        .read_to_end(&mut bytes)
        .map_err(|error| io_error("read inbox item", path, error))?;
    if u64::try_from(bytes.len()).unwrap_or(u64::MAX) > max_item_bytes {
        Ok(None)
    } else {
        Ok(Some(bytes))
    }
}

fn ensure_collect_paths(vault_root: &Path) -> Result<(), WikiError> {
    for relative in ["inbox", "raw", "raw/assets"] {
        let path = vault_root.join(relative);
        fs::create_dir_all(&path)
            .map_err(|error| io_error("create collect directory", &path, error))?;
    }

    let log = vault_root.join("log.md");
    if !log.exists() {
        fs::write(&log, "# Log\n\n").map_err(|error| io_error("write log", &log, error))?;
    }
    Ok(())
}

fn classify_inbox_item(path: &Path, bytes: &[u8]) -> Result<InboxKind, &'static str> {
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase);
    let text = text_from_utf8_lossy(bytes);

    match extension.as_deref() {
        Some("url" | "link" | "webloc") => extract_url(&text)
            .map(InboxKind::Url)
            .ok_or("url drop did not contain an http(s) URL"),
        Some("pdf") => Ok(InboxKind::File(SourceKind::Pdf)),
        Some("mp3" | "wav" | "m4a" | "aac" | "flac" | "ogg" | "opus") => {
            Ok(InboxKind::File(SourceKind::Audio))
        }
        Some("mp4" | "mov" | "m4v" | "webm" | "mkv") => Ok(InboxKind::File(SourceKind::Video)),
        Some("md" | "markdown") => Ok(InboxKind::File(SourceKind::Markdown)),
        Some("txt" | "text") => Ok(InboxKind::File(SourceKind::Text)),
        Some(_) => Ok(InboxKind::File(SourceKind::File)),
        None => extract_url(&text)
            .map(InboxKind::Url)
            .ok_or("extensionless inbox item is ambiguous"),
    }
}

fn accept_item(
    vault_root: &Path,
    fetched_at: &str,
    relative: String,
    path: PathBuf,
    bytes: Vec<u8>,
    kind: InboxKind,
    report: &mut CollectReport,
) -> Result<(), WikiError> {
    let previous_manifest = SourceManifest::read(vault_root)?;
    let (record_kind, raw_path, asset_path) = match kind {
        InboxKind::Url(url) => {
            let record = SourceManifest::register(
                vault_root,
                SourceDraft {
                    location: url.clone(),
                    kind: SourceKind::Url,
                    fetched_at: fetched_at.to_string(),
                    content: url.as_bytes().to_vec(),
                    title: Some(url.clone()),
                    citation: Some(url.clone()),
                    license: None,
                    ingestion_method: IngestionMethod::Manual,
                    compile_status: CompileStatus::Pending,
                },
            )?;
            let markdown = render_url_markdown(&url, fetched_at, &record.content_hash);
            let raw_path = match write_raw_markdown(vault_root, &record, &markdown) {
                Ok(raw_path) => raw_path,
                Err(error) => {
                    let predicted_raw_path = PathBuf::from("raw").join(format!("{}.md", record.id));
                    return rollback_registered_collect_source(
                        vault_root,
                        &previous_manifest,
                        Some(&predicted_raw_path),
                        None,
                        error,
                    );
                }
            };
            (record.kind, raw_path, None)
        }
        InboxKind::File(kind) => {
            let file_name = path
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("source");
            let title = markdown_title(file_name);
            let record = SourceManifest::register(
                vault_root,
                SourceDraft {
                    location: relative.clone(),
                    kind: kind.clone(),
                    fetched_at: fetched_at.to_string(),
                    content: bytes.clone(),
                    title: Some(title.clone()),
                    citation: Some(relative.clone()),
                    license: None,
                    ingestion_method: IngestionMethod::Manual,
                    compile_status: CompileStatus::Pending,
                },
            )?;
            let predicted_asset_path =
                should_store_asset(&kind).then(|| source_asset_path(&record, file_name));
            let asset_path = match predicted_asset_path.as_ref() {
                Some(predicted_asset_path) => {
                    match write_asset(vault_root, &record, file_name, &bytes) {
                        Ok(asset_path) => Some(asset_path),
                        Err(error) => {
                            return rollback_registered_collect_source(
                                vault_root,
                                &previous_manifest,
                                None,
                                Some(predicted_asset_path),
                                error,
                            );
                        }
                    }
                }
                None => None,
            };
            let markdown = render_file_markdown(
                &title,
                &relative,
                fetched_at,
                &record.content_hash,
                &kind,
                &bytes,
                asset_path.as_deref(),
            );
            let raw_path = match write_raw_markdown(vault_root, &record, &markdown) {
                Ok(raw_path) => raw_path,
                Err(error) => {
                    let predicted_raw_path = PathBuf::from("raw").join(format!("{}.md", record.id));
                    return rollback_registered_collect_source(
                        vault_root,
                        &previous_manifest,
                        Some(&predicted_raw_path),
                        asset_path.as_ref(),
                        error,
                    );
                }
            };
            (record.kind, raw_path, asset_path)
        }
    };

    if let Err(error) = fs::remove_file(&path) {
        let mut cleanup_errors = Vec::new();
        cleanup_collect_file(vault_root, &raw_path, &mut cleanup_errors);
        if let Some(asset_path) = &asset_path {
            cleanup_collect_file(vault_root, asset_path, &mut cleanup_errors);
        }
        let original_error = io_error("remove accepted inbox item", &path, error);
        if let Err(rollback_error) = previous_manifest.write(vault_root) {
            return Err(WikiError::Config {
                detail: format!(
                    "failed to roll back source manifest after inbox removal failed: {rollback_error}; original error: {original_error}{}",
                    collect_cleanup_detail(&cleanup_errors)
                ),
            });
        }
        return collect_error_with_cleanup(original_error, cleanup_errors);
    }
    report.accepted.push(CollectAction {
        inbox_path: relative,
        status: CollectStatus::Accepted,
        kind: Some(record_kind.to_string()),
        raw_path: Some(path_to_string(&raw_path)),
        reason: None,
    });
    Ok(())
}

fn rollback_registered_collect_source<T>(
    vault_root: &Path,
    previous_manifest: &SourceManifest,
    raw_path: Option<&PathBuf>,
    asset_path: Option<&PathBuf>,
    original_error: WikiError,
) -> Result<T, WikiError> {
    let mut cleanup_errors = Vec::new();
    if let Some(raw_path) = raw_path {
        cleanup_collect_file(vault_root, raw_path, &mut cleanup_errors);
    }
    if let Some(asset_path) = asset_path {
        cleanup_collect_file(vault_root, asset_path, &mut cleanup_errors);
    }
    if let Err(rollback_error) = previous_manifest.write(vault_root) {
        return Err(WikiError::Config {
            detail: format!(
                "failed to roll back source manifest after collect write failure: {rollback_error}; original error: {original_error}{}",
                collect_cleanup_detail(&cleanup_errors)
            ),
        });
    }
    collect_error_with_cleanup(original_error, cleanup_errors)
}

fn cleanup_collect_file(vault_root: &Path, relative_path: &Path, cleanup_errors: &mut Vec<String>) {
    let path = vault_root.join(relative_path);
    match fs::remove_file(&path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => cleanup_errors.push(format!("{}: {error}", path.display())),
    }
}

fn collect_error_with_cleanup<T>(
    original_error: WikiError,
    cleanup_errors: Vec<String>,
) -> Result<T, WikiError> {
    if cleanup_errors.is_empty() {
        return Err(original_error);
    }
    let cleanup_detail = collect_cleanup_detail(&cleanup_errors);
    Err(match original_error {
        WikiError::Config { detail } => WikiError::Config {
            detail: format!("{detail}{cleanup_detail}"),
        },
        WikiError::Io {
            action,
            path,
            source,
        } => WikiError::Io {
            action,
            path,
            source: std::io::Error::new(source.kind(), format!("{source}{cleanup_detail}")),
        },
        error => WikiError::Config {
            detail: format!("{error}{cleanup_detail}"),
        },
    })
}

fn collect_cleanup_detail(cleanup_errors: &[String]) -> String {
    if cleanup_errors.is_empty() {
        String::new()
    } else {
        format!("; cleanup failures: {}", cleanup_errors.join("; "))
    }
}

fn skip_item(
    vault_root: &Path,
    fetched_at: &str,
    relative: String,
    path: PathBuf,
    reason: impl Into<String>,
    report: &mut CollectReport,
) -> Result<(), WikiError> {
    let action = CollectAction {
        inbox_path: relative,
        status: CollectStatus::Skipped,
        kind: None,
        raw_path: None,
        reason: Some(reason.into()),
    };
    write_status_sidecar(vault_root, fetched_at, &path, &action)?;
    report.skipped.push(action);
    Ok(())
}

fn render_url_markdown(url: &str, fetched_at: &str, source_hash: &str) -> String {
    let mut markdown = markdown_metadata(&[
        ("source_kind", "url".to_string()),
        ("source_url", url.to_string()),
        ("fetched_at", fetched_at.to_string()),
        ("source_hash", source_hash.to_string()),
    ]);
    markdown.push_str("# ");
    markdown.push_str(url);
    markdown.push_str("\n\nCaptured URL: ");
    markdown.push_str(url);
    markdown.push('\n');
    markdown
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
        fields.push(("source_asset", path_to_string(asset_path)));
    }

    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(title);
    markdown.push_str("\n\n");

    match kind {
        SourceKind::Markdown | SourceKind::Text => {
            markdown.push_str(&text_from_utf8_lossy(bytes));
            if !markdown.ends_with('\n') {
                markdown.push('\n');
            }
        }
        _ => {
            if let Some(asset_path) = asset_path {
                markdown.push_str("Original artifact stored under `");
                markdown.push_str(&path_to_string(asset_path));
                markdown.push_str("`.\n");
            } else {
                markdown.push_str(
                    "Original artifact was recorded in the source manifest; no raw asset was written.\n",
                );
            }
        }
    }

    markdown
}

fn write_status_sidecar(
    vault_root: &Path,
    fetched_at: &str,
    path: &Path,
    action: &CollectAction,
) -> Result<(), WikiError> {
    let sidecar = status_sidecar_path(path);
    let payload = json!({
        "item": action.inbox_path,
        "status": action.status,
        "reason": action.reason,
        "checked_at": fetched_at,
    });
    let contents = serde_json::to_string_pretty(&payload).map_err(|error| WikiError::Json {
        action: "serialize inbox status",
        path: Some(vault_root.join(&action.inbox_path)),
        source: error,
    })? + "\n";
    fs::write(&sidecar, contents).map_err(|error| io_error("write inbox status", &sidecar, error))
}

fn append_log(
    vault_root: &Path,
    fetched_at: &str,
    report: &CollectReport,
) -> Result<(), WikiError> {
    let log_path = vault_root.join("log.md");
    let write_header = fs::metadata(&log_path).map_or(true, |metadata| metadata.len() == 0);
    let mut log = String::new();
    if write_header {
        log.push_str("# Log\n\n");
    } else {
        log.push('\n');
    }
    for action in &report.accepted {
        log.push_str("- ");
        log.push_str(fetched_at);
        log.push_str(" collect accepted `");
        log.push_str(&action.inbox_path);
        log.push('`');
        if let Some(kind) = &action.kind {
            log.push_str(" as `");
            log.push_str(kind);
            log.push('`');
        }
        if let Some(raw_path) = &action.raw_path {
            log.push_str(" -> `");
            log.push_str(raw_path);
            log.push('`');
        }
        log.push('\n');
    }
    for action in &report.skipped {
        log.push_str("- ");
        log.push_str(fetched_at);
        log.push_str(" collect skipped `");
        log.push_str(&action.inbox_path);
        log.push_str("`: ");
        log.push_str(action.reason.as_deref().unwrap_or("skipped"));
        log.push('\n');
    }
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|error| io_error("open log", &log_path, error))?;
    file.write_all(log.as_bytes())
        .map_err(|error| io_error("write log", &log_path, error))
}

fn extract_url(text: &str) -> Option<String> {
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        if let Some(value) = line.strip_prefix("URL=") {
            let value = value.trim();
            if is_http_url(value) {
                return Some(value.to_string());
            }
        }
        if is_http_url(line) {
            return Some(line.to_string());
        }
        if let Some(url) = urls_from_embedded_text(line).into_iter().next() {
            return Some(url);
        }
    }
    None
}

fn urls_from_embedded_text(text: &str) -> Vec<String> {
    let mut urls = Vec::new();
    let mut rest = text;
    while let Some(start) = rest.find("https://").or_else(|| rest.find("http://")) {
        let candidate = rest[start..]
            .split(|ch: char| ch.is_whitespace() || matches!(ch, '<' | '>' | '"' | '\''))
            .next()
            .unwrap_or("");
        if let Some(url) = parse_embedded_http_url(candidate) {
            urls.push(url);
        }
        rest = &rest[start + candidate.len()..];
    }
    urls
}

fn is_http_url(value: &str) -> bool {
    parse_http_url(value).is_some()
}

fn parse_embedded_http_url(candidate: &str) -> Option<String> {
    if let Some(trimmed) = trim_trailing_url_punctuation(candidate)
        && parse_http_url(trimmed).is_some()
    {
        return Some(trimmed.to_string());
    }
    parse_http_url(candidate).or_else(|| {
        let trimmed = candidate.trim_end_matches([',', '.', ';', ')', ']']);
        parse_http_url(trimmed).map(|_| trimmed.to_string())
    })
}

fn trim_trailing_url_punctuation(candidate: &str) -> Option<&str> {
    let trimmed = candidate.trim_end_matches([',', '.', ';', ')', ']']);
    let trailing = &candidate[trimmed.len()..];
    (trailing.chars().count() > 1).then_some(trimmed)
}

fn parse_http_url(value: &str) -> Option<String> {
    let parsed = url::Url::parse(value).ok()?;
    matches!(parsed.scheme(), "http" | "https").then(|| value.to_string())
}

fn should_store_asset(kind: &SourceKind) -> bool {
    matches!(
        kind,
        SourceKind::Audio | SourceKind::Pdf | SourceKind::Video | SourceKind::File
    )
}

fn is_status_sidecar(path: &Path) -> bool {
    path.file_name()
        .and_then(|value| value.to_str())
        .is_some_and(|name| name.ends_with(".status.json"))
}

fn status_sidecar_path(path: &Path) -> PathBuf {
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("item");
    path.with_file_name(format!("{file_name}.status.json"))
}

fn relative_to_vault(vault_root: &Path, path: &Path) -> String {
    path.strip_prefix(vault_root)
        .map(path_to_string)
        .unwrap_or_else(|_| path_to_string(path))
}

fn path_to_string(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn io_error(action: &'static str, path: &Path, error: std::io::Error) -> WikiError {
    WikiError::Io {
        action,
        path: Some(path.to_path_buf()),
        source: error,
    }
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::path::{Path, PathBuf};

    use super::*;
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::{MemoryWikiStore, WikiDocumentKind, WikiIngestionEvent};

    fn write_file(root: &Path, relative: &str, contents: &[u8]) {
        let path = root.join(relative);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).expect("create parent");
        }
        fs::write(path, contents).expect("write file");
    }

    #[test]
    fn collect_routes_known_items() {
        let temp = tempfile::tempdir().expect("tempdir");
        write_file(
            temp.path(),
            "inbox/link.url",
            b"[InternetShortcut]\nURL=https://example.com/research\n",
        );
        write_file(temp.path(), "inbox/paper.pdf", b"%PDF-1.7\nsource\n%%EOF\n");
        write_file(
            temp.path(),
            "inbox/notes.md",
            b"# Notes\n\nMarkdown source.\n",
        );
        write_file(temp.path(), "inbox/plain.txt", b"plain source text\n");
        write_file(temp.path(), "inbox/interview.wav", b"RIFF....WAVEaudio");
        write_file(temp.path(), "inbox/data.csv", b"name,value\nalpha,1\n");

        let report =
            collect_inbox(temp.path(), "2026-05-29T18:00:00Z").expect("collect inbox items");

        assert_eq!(report.accepted.len(), 6);
        assert!(report.skipped.is_empty());
        for name in [
            "link.url",
            "paper.pdf",
            "notes.md",
            "plain.txt",
            "interview.wav",
            "data.csv",
        ] {
            assert!(
                !temp.path().join("inbox").join(name).exists(),
                "accepted inbox item should move out: {name}"
            );
        }

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        let kinds = manifest
            .entries
            .iter()
            .map(|entry| entry.kind.clone())
            .collect::<Vec<_>>();
        for kind in [
            SourceKind::Url,
            SourceKind::Pdf,
            SourceKind::Markdown,
            SourceKind::Text,
            SourceKind::Audio,
            SourceKind::File,
        ] {
            assert!(kinds.contains(&kind), "manifest contains {kind}");
        }
        for entry in manifest.entries {
            assert!(
                temp.path()
                    .join("raw")
                    .join(format!("{}.md", entry.id))
                    .is_file(),
                "raw markdown exists for {}",
                entry.location
            );
        }
    }

    #[test]
    fn collect_indexes_accepted_sources() {
        let temp = tempfile::tempdir().expect("tempdir");
        write_file(temp.path(), "inbox/note.txt", b"accepted source text\n");
        let mut store = MemoryWikiStore::default();

        let report = collect_inbox_and_index(temp.path(), &mut store, "2026-05-29T18:03:00Z")
            .expect("collect and index inbox items");

        assert_eq!(report.accepted.len(), 1);
        let raw_path = PathBuf::from(
            report.accepted[0]
                .raw_path
                .as_deref()
                .expect("accepted raw path"),
        );
        assert!(temp.path().join(&raw_path).is_file());

        let catalog_path = PathBuf::from("raw/INDEX.md");
        let catalog = store
            .documents
            .get(&catalog_path)
            .expect("raw source catalog indexed");
        assert_eq!(catalog.kind, WikiDocumentKind::SourceCatalog);
        assert!(catalog.body.contains("inbox/note.txt"));
        assert!(catalog.body.contains("kind: `text`"));
        assert!(store.sources.contains_key(&catalog_path));
        assert!(store.ingestions.iter().any(|ingestion| {
            ingestion.path == catalog_path && ingestion.event == WikiIngestionEvent::Added
        }));
    }

    #[test]
    fn embedded_url_parser_returns_all_urls_in_order() {
        assert_eq!(
            urls_from_embedded_text(
                "Sources: https://example.test/one, then http://example.test/two."
            ),
            vec![
                "https://example.test/one,".to_string(),
                "http://example.test/two.".to_string()
            ]
        );
    }

    #[test]
    fn embedded_url_parser_preserves_valid_punctuation_before_trimming() {
        assert_eq!(
            urls_from_embedded_text("See https://example.test/path_(v1) for details"),
            vec!["https://example.test/path_(v1)".to_string()]
        );
    }

    #[test]
    fn embedded_url_parser_returns_trimmed_candidate_when_trimmed_parse_succeeds() {
        assert_eq!(
            urls_from_embedded_text("See [https://example.test/research]."),
            vec!["https://example.test/research".to_string()]
        );
    }

    #[test]
    fn ambiguous_items_remain_in_inbox() {
        let temp = tempfile::tempdir().expect("tempdir");
        write_file(temp.path(), "inbox/untitled", b"ambiguous dropped text\n");

        let report =
            collect_inbox(temp.path(), "2026-05-29T18:05:00Z").expect("collect inbox items");

        assert!(report.accepted.is_empty());
        assert_eq!(report.skipped.len(), 1);
        assert!(temp.path().join("inbox/untitled").is_file());
        let status_path = temp.path().join("inbox/untitled.status.json");
        let status = fs::read_to_string(status_path).expect("status sidecar");
        assert!(status.contains("\"status\""));
        assert!(status.contains("\"skipped\""));
        assert!(status.contains("extensionless inbox item is ambiguous"));
    }

    #[test]
    fn collect_logs_actions() {
        let temp = tempfile::tempdir().expect("tempdir");
        write_file(temp.path(), "inbox/note.txt", b"accepted text\n");
        write_file(temp.path(), "inbox/mystery", b"ambiguous text\n");

        collect_inbox(temp.path(), "2026-05-29T18:10:00Z").expect("collect inbox items");

        let log = fs::read_to_string(temp.path().join("log.md")).expect("read log");
        assert!(log.contains("2026-05-29T18:10:00Z collect accepted"));
        assert!(log.contains("inbox/note.txt"));
        assert!(log.contains("2026-05-29T18:10:00Z collect skipped"));
        assert!(log.contains("inbox/mystery"));
    }

    #[test]
    fn oversized_items_are_skipped_before_reading() {
        let temp = tempfile::tempdir().expect("tempdir");
        write_file(temp.path(), "inbox/large.txt", b"too large");

        let report = collect_inbox_with_limit(temp.path(), "2026-05-29T18:12:00Z", 3)
            .expect("collect inbox items");

        assert!(report.accepted.is_empty());
        assert_eq!(report.skipped.len(), 1);
        assert_eq!(
            report.skipped[0].reason.as_deref(),
            Some("inbox item exceeds 3 byte limit")
        );
        assert!(temp.path().join("inbox/large.txt").is_file());
    }

    #[test]
    fn collect_cleanup_context_preserves_config_error_variant() {
        let error = collect_error_with_cleanup::<()>(
            WikiError::Config {
                detail: "write failed".to_string(),
            },
            vec!["remove raw failed".to_string()],
        )
        .expect_err("cleanup error must be returned");

        match error {
            WikiError::Config { detail } => {
                assert!(detail.contains("write failed"));
                assert!(detail.contains("cleanup failures: remove raw failed"));
            }
            other => panic!("expected config error, got {other:?}"),
        }
    }

    #[test]
    fn collect_cleanup_context_preserves_io_error_variant() {
        let error = collect_error_with_cleanup::<()>(
            WikiError::Io {
                action: "write raw source",
                path: None,
                source: std::io::Error::new(std::io::ErrorKind::PermissionDenied, "denied"),
            },
            vec!["remove raw failed".to_string()],
        )
        .expect_err("cleanup error must be returned");

        match error {
            WikiError::Io { source, .. } => {
                assert_eq!(source.kind(), std::io::ErrorKind::PermissionDenied);
                assert!(source.to_string().contains("denied"));
                assert!(
                    source
                        .to_string()
                        .contains("cleanup failures: remove raw failed")
                );
            }
            other => panic!("expected io error, got {other:?}"),
        }
    }
}

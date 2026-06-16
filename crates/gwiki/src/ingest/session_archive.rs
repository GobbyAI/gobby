use std::collections::HashSet;
use std::fs::{self, File};
use std::io::{ErrorKind, Read};
use std::path::{Path, PathBuf};

use flate2::read::GzDecoder;

use crate::WikiError;
use crate::ingest::{IngestResult, index_after_ingest};
use crate::sources::{SourceKind, SourceManifest};
use crate::store::WikiIndexStore;

use super::session::{SessionFileSnapshot, ingest_session_file_without_index};

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct AcceptedSessionArchive {
    pub(crate) archive_path: PathBuf,
    pub(crate) result: IngestResult,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SkippedSessionArchive {
    pub(crate) archive_path: PathBuf,
    pub(crate) content_hash: String,
    pub(crate) reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SessionArchiveFailure {
    pub(crate) archive_path: PathBuf,
    pub(crate) code: String,
    pub(crate) message: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct SessionArchiveBatchIngest {
    pub(crate) archive_dir: PathBuf,
    pub(crate) scanned: usize,
    pub(crate) accepted: Vec<AcceptedSessionArchive>,
    pub(crate) skipped: Vec<SkippedSessionArchive>,
    pub(crate) failed: Vec<SessionArchiveFailure>,
}

impl SessionArchiveBatchIngest {
    pub(crate) fn status(&self) -> &'static str {
        match (
            self.accepted.is_empty(),
            self.skipped.is_empty(),
            self.failed.is_empty(),
            self.scanned == 0,
        ) {
            (_, _, true, true) => "empty",
            (true, false, true, false) => "skipped",
            (false, _, true, _) => "ingested",
            (true, true, false, _) => "failed",
            _ => "partial",
        }
    }

    pub(crate) fn exit_code(&self) -> u8 {
        u8::from(!self.failed.is_empty() && self.accepted.is_empty())
    }
}

pub(crate) fn sync_session_transcript_archives(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    archive_dir: &Path,
    limit: Option<usize>,
    fetched_at: &str,
) -> Result<SessionArchiveBatchIngest, WikiError> {
    if matches!(limit, Some(0)) {
        return Err(WikiError::InvalidInput {
            field: "sync-sessions.limit",
            message: "limit must be greater than 0".to_string(),
        });
    }

    let mut paths = session_archive_paths(archive_dir)?;
    if let Some(limit) = limit {
        paths.truncate(limit);
    }

    let manifest = SourceManifest::read(vault_root)?;
    let mut known_session_hashes = manifest
        .entries
        .iter()
        .filter(|entry| entry.kind == SourceKind::Session)
        .map(|entry| entry.content_hash.clone())
        .collect::<HashSet<_>>();
    let scanned = paths.len();
    let mut accepted = Vec::new();
    let mut skipped = Vec::new();
    let mut failed = Vec::new();

    for path in paths {
        let bytes = match read_gzipped_archive(&path) {
            Ok(bytes) => bytes,
            Err(failure) => {
                failed.push(failure);
                continue;
            }
        };
        let content_hash = gobby_core::indexing::content_hash(&bytes);
        if known_session_hashes.contains(&content_hash) {
            skipped.push(SkippedSessionArchive {
                archive_path: path,
                content_hash,
                reason: "content_hash_already_ingested".to_string(),
            });
            continue;
        }

        let file_name = archive_file_name(&path);
        let snapshot = SessionFileSnapshot {
            location: format!("session_transcripts/{file_name}"),
            file_name,
            fetched_at: fetched_at.to_string(),
            path: path.clone(),
            bytes,
        };
        match ingest_session_file_without_index(vault_root, snapshot) {
            Ok(result) => {
                known_session_hashes.insert(result.record.content_hash.clone());
                accepted.push(AcceptedSessionArchive {
                    archive_path: path,
                    result,
                });
            }
            Err(error) => failed.push(SessionArchiveFailure::from_wiki_error(&path, error)),
        }
    }

    if !accepted.is_empty() {
        index_after_ingest(vault_root, store)?;
    }

    Ok(SessionArchiveBatchIngest {
        archive_dir: archive_dir.to_path_buf(),
        scanned,
        accepted,
        skipped,
        failed,
    })
}

fn session_archive_paths(archive_dir: &Path) -> Result<Vec<PathBuf>, WikiError> {
    let mut paths = Vec::new();
    let entries = match fs::read_dir(archive_dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(paths),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read session transcript archive directory",
                path: Some(archive_dir.to_path_buf()),
                source: error,
            });
        }
    };
    for entry in entries {
        let entry = entry.map_err(|error| WikiError::Io {
            action: "read session transcript archive directory entry",
            path: Some(archive_dir.to_path_buf()),
            source: error,
        })?;
        let path = entry.path();
        if path.is_file() && is_jsonl_gz(&path) {
            paths.push(path);
        }
    }
    paths.sort();
    Ok(paths)
}

fn is_jsonl_gz(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.ends_with(".jsonl.gz"))
}

fn read_gzipped_archive(path: &Path) -> Result<Vec<u8>, SessionArchiveFailure> {
    let file = File::open(path).map_err(|error| {
        SessionArchiveFailure::new(path, "io", format!("failed to open archive: {error}"))
    })?;
    let mut decoder = GzDecoder::new(file);
    let mut bytes = Vec::new();
    decoder.read_to_end(&mut bytes).map_err(|error| {
        SessionArchiveFailure::new(
            path,
            "gzip_decode",
            format!("failed to decompress archive: {error}"),
        )
    })?;
    Ok(bytes)
}

fn archive_file_name(path: &Path) -> String {
    path.file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("session.jsonl.gz")
        .to_string()
}

impl SessionArchiveFailure {
    fn new(path: &Path, code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            archive_path: path.to_path_buf(),
            code: code.into(),
            message: message.into(),
        }
    }

    fn from_wiki_error(path: &Path, error: WikiError) -> Self {
        Self::new(path, error.code(), error.to_string())
    }
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use flate2::Compression;
    use flate2::write::GzEncoder;

    use super::*;
    use crate::store::MemoryWikiStore;

    #[test]
    fn sync_session_archives_ingests_gzip_and_indexes_once() {
        let temp = tempfile::tempdir().expect("tempdir");
        let archive_dir = temp.path().join("session_transcripts");
        fs::create_dir(&archive_dir).expect("archive dir");
        write_archive(
            &archive_dir.join("one.jsonl.gz"),
            br#"{"type":"session","timestamp":"2026-06-16T20:00:00Z","payload":{"title":"Archive import","messages":[{"role":"user","content":"Summarize the project."},{"role":"assistant","content":"The session archive is searchable."}]}}"#,
        );

        let mut store = MemoryWikiStore::default();
        let report = sync_session_transcript_archives(
            temp.path(),
            &mut store,
            &archive_dir,
            None,
            "2026-06-16T20:05:00Z",
        )
        .expect("sync archives");

        assert_eq!(report.status(), "ingested");
        assert_eq!(report.scanned, 1);
        assert_eq!(report.accepted.len(), 1);
        assert!(report.skipped.is_empty());
        assert!(report.failed.is_empty());
        let raw_markdown =
            fs::read_to_string(temp.path().join(&report.accepted[0].result.raw_path))
                .expect("raw markdown");
        let derived_markdown = fs::read_to_string(
            temp.path()
                .join("knowledge")
                .join("sources")
                .join(format!("{}.md", report.accepted[0].result.record.id)),
        )
        .expect("derived markdown");
        assert!(raw_markdown.contains("Archive import"));
        assert!(raw_markdown.contains("session archive is searchable"));
        assert!(derived_markdown.contains("Archive import"));
        assert!(!store.documents.is_empty());
        assert!(!store.chunks.is_empty());
        assert!(indexed_store_text(&store).contains("session archive is searchable"));
    }

    #[test]
    fn sync_session_archives_treats_missing_archive_dir_as_empty() {
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = MemoryWikiStore::default();
        let report = sync_session_transcript_archives(
            temp.path(),
            &mut store,
            &temp.path().join("missing-session-transcripts"),
            None,
            "2026-06-16T20:05:00Z",
        )
        .expect("sync missing archive dir");

        assert_eq!(report.status(), "empty");
        assert_eq!(report.scanned, 0);
        assert!(report.accepted.is_empty());
        assert!(report.skipped.is_empty());
        assert!(report.failed.is_empty());
        assert_eq!(report.exit_code(), 0);
    }

    #[test]
    fn sync_session_archives_skips_previously_ingested_hashes() {
        let temp = tempfile::tempdir().expect("tempdir");
        let archive_dir = temp.path().join("session_transcripts");
        fs::create_dir(&archive_dir).expect("archive dir");
        let archive_path = archive_dir.join("repeat.jsonl.gz");
        write_archive(
            &archive_path,
            br#"{"type":"session","timestamp":"2026-06-16T20:00:00Z","payload":{"title":"Repeat import","messages":[{"role":"user","content":"First run."}]}}"#,
        );

        let mut store = MemoryWikiStore::default();
        let first = sync_session_transcript_archives(
            temp.path(),
            &mut store,
            &archive_dir,
            None,
            "2026-06-16T20:05:00Z",
        )
        .expect("first sync");
        assert_eq!(first.accepted.len(), 1);

        let mut second_store = MemoryWikiStore::default();
        let second = sync_session_transcript_archives(
            temp.path(),
            &mut second_store,
            &archive_dir,
            None,
            "2026-06-16T20:06:00Z",
        )
        .expect("second sync");

        assert_eq!(second.status(), "skipped");
        assert!(second.accepted.is_empty());
        assert_eq!(second.skipped.len(), 1);
        assert_eq!(second.skipped[0].archive_path, archive_path);
        assert_eq!(second.skipped[0].reason, "content_hash_already_ingested");
        assert!(second_store.documents.is_empty());
    }

    #[test]
    fn sync_session_archives_reports_bad_gzip_without_blocking_good_archives() {
        let temp = tempfile::tempdir().expect("tempdir");
        let archive_dir = temp.path().join("session_transcripts");
        fs::create_dir(&archive_dir).expect("archive dir");
        write_archive(
            &archive_dir.join("good.jsonl.gz"),
            br#"{"type":"session","timestamp":"2026-06-16T20:00:00Z","payload":{"title":"Good import","messages":[{"role":"user","content":"Good archive."}]}}"#,
        );
        fs::write(archive_dir.join("bad.jsonl.gz"), b"not gzip").expect("bad archive");

        let mut store = MemoryWikiStore::default();
        let report = sync_session_transcript_archives(
            temp.path(),
            &mut store,
            &archive_dir,
            None,
            "2026-06-16T20:05:00Z",
        )
        .expect("sync archives");

        assert_eq!(report.status(), "partial");
        assert_eq!(report.accepted.len(), 1);
        assert_eq!(report.failed.len(), 1);
        assert_eq!(report.failed[0].code, "gzip_decode");
        assert_eq!(report.exit_code(), 0);
    }

    fn write_archive(path: &Path, bytes: &[u8]) {
        let file = File::create(path).expect("create archive");
        let mut encoder = GzEncoder::new(file, Compression::default());
        encoder.write_all(bytes).expect("write archive");
        encoder.finish().expect("finish archive");
    }

    fn indexed_store_text(store: &MemoryWikiStore) -> String {
        let mut text = String::new();
        for document in store.documents.values() {
            text.push_str(&document.body);
            text.push('\n');
        }
        for chunks in store.chunks.values() {
            for chunk in chunks {
                text.push_str(&chunk.content);
                text.push('\n');
            }
        }
        text
    }
}

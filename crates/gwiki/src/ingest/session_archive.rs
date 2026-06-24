use std::collections::HashSet;
use std::fs::{self, File};
use std::io::{ErrorKind, Read};
use std::path::{Path, PathBuf};

use flate2::read::GzDecoder;

use crate::WikiError;
use crate::ingest::{IngestResult, index_after_ingest};
use crate::paths::{derived_markdown_path, raw_source_path};
use crate::sources::{SourceKind, SourceManifest};
use crate::store::WikiIndexStore;

use super::session::{
    SessionFileSnapshot, SessionWikiFileSnapshot, ingest_session_file_without_index,
    ingest_session_wiki_file_without_index,
};

/// Compound suffix of daemon-synthesized session wiki files in the wiki dir.
const SESSION_WIKI_SUFFIX: &str = ".md";
/// Compound suffix of raw session transcript archives in the archive dir.
const SESSION_ARCHIVE_SUFFIX: &str = ".jsonl.gz";

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

/// One discovered session source: a daemon-synthesized wiki page (authoritative)
/// or a raw transcript archive (fallback). Synthesis is sourced first so it can
/// supersede the raw page for the same session.
enum SessionSourceFile {
    Synthesis(PathBuf),
    RawArchive(PathBuf),
}

pub(crate) fn sync_session_transcript_archives(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    archive_dir: &Path,
    wiki_dir: &Path,
    limit: Option<usize>,
    fetched_at: &str,
) -> Result<SessionArchiveBatchIngest, WikiError> {
    if matches!(limit, Some(0)) {
        return Err(WikiError::InvalidInput {
            field: "sync-sessions.limit",
            message: "limit must be greater than 0".to_string(),
        });
    }

    // Daemon synthesis is the authoritative source. Collect it first and key
    // suppression on the bare external id (the `.md` stem) so a raw archive whose
    // session already has a synthesized page is never re-parsed into a second page.
    let synthesis_paths = session_wiki_paths(wiki_dir)?;
    let synthesized = synthesis_paths
        .iter()
        .filter_map(|path| session_external_id(path, SESSION_WIKI_SUFFIX))
        .collect::<HashSet<_>>();

    let mut work = synthesis_paths
        .into_iter()
        .map(SessionSourceFile::Synthesis)
        .collect::<Vec<_>>();
    work.extend(
        session_archive_paths(archive_dir)?
            .into_iter()
            .map(SessionSourceFile::RawArchive),
    );
    if let Some(limit) = limit {
        work.truncate(limit);
    }

    let manifest = SourceManifest::read(vault_root)?;
    let mut known_session_hashes = manifest
        .entries
        .iter()
        .filter(|entry| entry.kind == SourceKind::Session)
        .map(|entry| entry.content_hash.clone())
        .collect::<HashSet<_>>();
    let scanned = work.len();
    let mut accepted = Vec::new();
    let mut skipped = Vec::new();
    let mut failed = Vec::new();

    for item in work {
        match item {
            SessionSourceFile::Synthesis(path) => {
                let Some(external_id) = session_external_id(&path, SESSION_WIKI_SUFFIX) else {
                    failed.push(SessionArchiveFailure::new(
                        &path,
                        "session_wiki_name",
                        "session wiki file name has no external id stem",
                    ));
                    continue;
                };
                let bytes = match fs::read(&path) {
                    Ok(bytes) => bytes,
                    Err(error) => {
                        failed.push(SessionArchiveFailure::new(
                            &path,
                            "session_wiki_read",
                            format!("failed to read session wiki file: {error}"),
                        ));
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
                if let Err(error) = supersede_session_page(vault_root, &external_id, &content_hash)
                {
                    failed.push(SessionArchiveFailure::from_wiki_error(&path, error));
                    continue;
                }
                let snapshot = SessionWikiFileSnapshot {
                    external_id,
                    path: path.clone(),
                    fetched_at: fetched_at.to_string(),
                    bytes,
                };
                match ingest_session_wiki_file_without_index(vault_root, snapshot) {
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
            SessionSourceFile::RawArchive(path) => {
                let external_id = session_external_id(&path, SESSION_ARCHIVE_SUFFIX);
                if let Some(external_id) = &external_id
                    && synthesized.contains(external_id)
                {
                    skipped.push(SkippedSessionArchive {
                        archive_path: path,
                        content_hash: String::new(),
                        reason: "superseded_by_synthesis".to_string(),
                    });
                    continue;
                }
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
                // Key the raw fallback on the canonical `session:{external_id}` too,
                // so a later synthesis (or re-run) supersedes this page. `file_name`
                // still seeds the title and the `source_archive` provenance field.
                let external_id = external_id.unwrap_or_else(|| file_name.clone());
                if let Err(error) = supersede_session_page(vault_root, &external_id, &content_hash)
                {
                    failed.push(SessionArchiveFailure::from_wiki_error(&path, error));
                    continue;
                }
                let snapshot = SessionFileSnapshot {
                    location: format!("session:{external_id}"),
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

/// Bare session external id from a session file name, stripping the *full*
/// compound suffix (`abc.jsonl.gz` → `abc`, not `abc.jsonl` as `Path::file_stem`
/// would). `None` for names without the suffix or with an empty stem.
fn session_external_id(path: &Path, suffix: &str) -> Option<String> {
    let name = path.file_name().and_then(|name| name.to_str())?;
    let stem = name.strip_suffix(suffix)?;
    (!stem.is_empty()).then(|| stem.to_string())
}

/// Lists daemon-synthesized session wiki files (`*.md`) in `wiki_dir`, sorted.
/// A missing directory is treated as empty.
fn session_wiki_paths(wiki_dir: &Path) -> Result<Vec<PathBuf>, WikiError> {
    let mut paths = Vec::new();
    let entries = match fs::read_dir(wiki_dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(paths),
        Err(error) => {
            return Err(WikiError::Io {
                action: "read session wiki directory",
                path: Some(wiki_dir.to_path_buf()),
                source: error,
            });
        }
    };
    for entry in entries {
        let entry = entry.map_err(|error| WikiError::Io {
            action: "read session wiki directory entry",
            path: Some(wiki_dir.to_path_buf()),
            source: error,
        })?;
        let path = entry.path();
        if path.is_file() && is_session_wiki_md(&path) {
            paths.push(path);
        }
    }
    paths.sort();
    Ok(paths)
}

fn is_session_wiki_md(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.ends_with(SESSION_WIKI_SUFFIX))
}

/// Latest-wins replacement: if a prior page already exists for this session under
/// the canonical `session:{external_id}` location (or the legacy raw archive-path
/// location used before #926) with a *different* content hash, remove its manifest
/// entry and derived/raw pages so the fresh content fully replaces it. A matching
/// hash is left untouched — the page is already current. Exactly one page per
/// session, and a fresh synthesis can supersede a previously raw-parsed page.
fn supersede_session_page(
    vault_root: &Path,
    external_id: &str,
    new_content_hash: &str,
) -> Result<(), WikiError> {
    let manifest = SourceManifest::read(vault_root)?;
    let canonical = format!("session:{external_id}");
    let legacy = format!("session_transcripts/{external_id}{SESSION_ARCHIVE_SUFFIX}");
    let existing = manifest.entries.iter().find(|entry| {
        entry.kind == SourceKind::Session
            && entry.content_hash != new_content_hash
            && (entry.canonical_location == canonical || entry.canonical_location == legacy)
    });
    let Some(existing) = existing else {
        return Ok(());
    };
    remove_vault_file(vault_root, &derived_markdown_path(existing)?)?;
    remove_vault_file(vault_root, &raw_source_path(&existing.id)?)?;
    SourceManifest::remove(vault_root, &existing.id)?;
    Ok(())
}

/// Removes a vault-relative file if present; a missing file is not an error.
fn remove_vault_file(vault_root: &Path, relative: &Path) -> Result<(), WikiError> {
    let path = vault_root.join(relative);
    match fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(()),
        Err(error) => Err(WikiError::Io {
            action: "remove superseded session page",
            path: Some(path),
            source: error,
        }),
    }
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
    use crate::sources::{CompileStatus, IngestionMethod, SourceDraftRef};
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
            &temp.path().join("session_wiki"),
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
            &temp.path().join("missing-session-wiki"),
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
            &temp.path().join("session_wiki"),
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
            &temp.path().join("session_wiki"),
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
            &temp.path().join("session_wiki"),
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

    /// Writes a daemon-shaped session wiki file (frontmatter + synthesized body).
    fn write_session_wiki(wiki_dir: &Path, external_id: &str, body: &str) -> PathBuf {
        let page = format!(
            "---\ntitle: \"Session: {short} — 2026-06-24\"\ntype: source\ntags: [alpha, beta]\nsession_id: {external_id}\nproject: demo\nsource: claude\nmodel: claude-opus-4-8\n---\n\n{body}\n",
            short = &external_id[..external_id.len().min(8)],
        );
        let path = wiki_dir.join(format!("{external_id}.md"));
        fs::write(&path, page).expect("write session wiki");
        path
    }

    #[test]
    fn synthesis_first_ingests_wiki_page() {
        let temp = tempfile::tempdir().expect("tempdir");
        let vault = temp.path();
        let wiki_dir = vault.join("session_wiki");
        fs::create_dir(&wiki_dir).expect("wiki dir");
        let external_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
        write_session_wiki(
            &wiki_dir,
            external_id,
            "## Summary\n\nShipped the synthesis-first sourcing loop.\n\n## Key Claims\n\n- The daemon synthesizes one concise page per session.\n\n## Key Quotes\n\n> Synthesis supersedes raw.\n\n## Connections\n\n- [[session-transcript-wiki-fix]]\n\n## Contradictions\n\nNone.\n",
        );

        let mut store = MemoryWikiStore::default();
        let report = sync_session_transcript_archives(
            vault,
            &mut store,
            &vault.join("missing-archives"),
            &wiki_dir,
            None,
            "2026-06-24T00:00:00Z",
        )
        .expect("sync");

        assert_eq!(report.status(), "ingested");
        assert_eq!(report.scanned, 1);
        assert_eq!(report.accepted.len(), 1);
        assert!(report.skipped.is_empty());
        assert!(report.failed.is_empty());

        let record = &report.accepted[0].result.record;
        assert_eq!(record.canonical_location, format!("session:{external_id}"));
        let derived = fs::read_to_string(
            vault
                .join("knowledge")
                .join("sources")
                .join(format!("{}.md", record.id)),
        )
        .expect("derived markdown");
        // Synthesized page: H1 title + the daemon body sections + >=1 wikilink,
        // never a transcript (criterion 4: ## Summary / ## Key Claims /
        // ## Connections + [[wikilink]], no ## Messages).
        assert!(derived.contains("# Session: "));
        assert!(derived.contains("## Summary"));
        assert!(derived.contains("## Key Claims"));
        assert!(derived.contains("## Connections"));
        assert!(derived.contains("[[session-transcript-wiki-fix]]"));
        assert!(!derived.contains("## Messages"));
        assert!(derived.contains(&format!("session:{external_id}")));
        assert!(derived.contains("source_archive:"));
        assert!(indexed_store_text(&store).contains("Shipped the synthesis-first sourcing loop"));
    }

    #[test]
    fn raw_archive_without_synthesis_uses_session_location() {
        let temp = tempfile::tempdir().expect("tempdir");
        let vault = temp.path();
        let archive_dir = vault.join("session_transcripts");
        fs::create_dir(&archive_dir).expect("archive dir");
        let external_id = "11112222-3333-4444-5555-666677778888";
        write_archive(
            &archive_dir.join(format!("{external_id}.jsonl.gz")),
            br#"{"type":"session","timestamp":"2026-06-24T00:00:00Z","payload":{"title":"Raw fallback","messages":[{"role":"user","content":"No synthesis here."}]}}"#,
        );

        let mut store = MemoryWikiStore::default();
        let report = sync_session_transcript_archives(
            vault,
            &mut store,
            &archive_dir,
            &vault.join("missing-session-wiki"),
            None,
            "2026-06-24T00:05:00Z",
        )
        .expect("sync");

        assert_eq!(report.accepted.len(), 1);
        assert!(report.skipped.is_empty());
        let record = &report.accepted[0].result.record;
        // Raw fallback now keys on the canonical session location, not the path.
        assert_eq!(record.canonical_location, format!("session:{external_id}"));
        let derived = fs::read_to_string(
            vault
                .join("knowledge")
                .join("sources")
                .join(format!("{}.md", record.id)),
        )
        .expect("derived markdown");
        assert!(derived.contains(&format!("session:{external_id}")));
        assert!(derived.contains("source_archive:"));
        assert!(derived.contains("## Messages"));
    }

    #[test]
    fn synthesis_suppresses_matching_raw_archive() {
        let temp = tempfile::tempdir().expect("tempdir");
        let vault = temp.path();
        let external_id = "dddddddd-1111-2222-3333-444455556666";

        let archive_dir = vault.join("session_transcripts");
        fs::create_dir(&archive_dir).expect("archive dir");
        write_archive(
            &archive_dir.join(format!("{external_id}.jsonl.gz")),
            br#"{"type":"session","timestamp":"2026-06-24T00:00:00Z","payload":{"title":"Raw duplicate","messages":[{"role":"user","content":"Verbose transcript."}]}}"#,
        );

        let wiki_dir = vault.join("session_wiki");
        fs::create_dir(&wiki_dir).expect("wiki dir");
        write_session_wiki(&wiki_dir, external_id, "## Summary\n\nConcise synthesis.\n");

        let mut store = MemoryWikiStore::default();
        let report = sync_session_transcript_archives(
            vault,
            &mut store,
            &archive_dir,
            &wiki_dir,
            None,
            "2026-06-24T00:05:00Z",
        )
        .expect("sync");

        assert_eq!(report.scanned, 2);
        assert_eq!(report.accepted.len(), 1);
        assert!(
            report.accepted[0]
                .archive_path
                .ends_with(format!("{external_id}.md"))
        );
        assert_eq!(report.skipped.len(), 1);
        assert_eq!(report.skipped[0].reason, "superseded_by_synthesis");
        assert!(
            report.skipped[0]
                .archive_path
                .ends_with(format!("{external_id}.jsonl.gz"))
        );

        let manifest = SourceManifest::read(vault).expect("manifest");
        let sessions: Vec<_> = manifest
            .entries
            .iter()
            .filter(|entry| entry.kind == SourceKind::Session)
            .collect();
        assert_eq!(sessions.len(), 1);
        assert_eq!(
            sessions[0].canonical_location,
            format!("session:{external_id}")
        );
        // The verbose raw transcript never reached the index.
        assert!(!indexed_store_text(&store).contains("Verbose transcript"));
        assert!(indexed_store_text(&store).contains("Concise synthesis"));
    }

    #[test]
    fn fresh_synthesis_supersedes_previous_synthesis_page() {
        let temp = tempfile::tempdir().expect("tempdir");
        let vault = temp.path();
        let wiki_dir = vault.join("session_wiki");
        fs::create_dir(&wiki_dir).expect("wiki dir");
        let external_id = "abcdef00-1111-2222-3333-444455556666";

        write_session_wiki(&wiki_dir, external_id, "## Summary\n\nFirst revision.\n");
        let mut store = MemoryWikiStore::default();
        let first = sync_session_transcript_archives(
            vault,
            &mut store,
            &vault.join("missing-archives"),
            &wiki_dir,
            None,
            "2026-06-24T00:00:00Z",
        )
        .expect("first sync");
        assert_eq!(first.accepted.len(), 1);
        let old_id = first.accepted[0].result.record.id.clone();
        let old_derived = vault
            .join("knowledge")
            .join("sources")
            .join(format!("{old_id}.md"));
        assert!(old_derived.exists());

        // The daemon regenerates the synthesis with new content.
        write_session_wiki(&wiki_dir, external_id, "## Summary\n\nSecond revision.\n");
        let mut store = MemoryWikiStore::default();
        let second = sync_session_transcript_archives(
            vault,
            &mut store,
            &vault.join("missing-archives"),
            &wiki_dir,
            None,
            "2026-06-24T01:00:00Z",
        )
        .expect("second sync");

        assert_eq!(second.accepted.len(), 1);
        let new_id = second.accepted[0].result.record.id.clone();
        assert_ne!(new_id, old_id);
        assert!(
            !old_derived.exists(),
            "old derived page removed on replacement"
        );
        let new_derived = fs::read_to_string(
            vault
                .join("knowledge")
                .join("sources")
                .join(format!("{new_id}.md")),
        )
        .expect("new derived");
        assert!(new_derived.contains("Second revision"));
        assert!(!new_derived.contains("First revision"));

        let manifest = SourceManifest::read(vault).expect("manifest");
        let sessions: Vec<_> = manifest
            .entries
            .iter()
            .filter(|entry| entry.kind == SourceKind::Session)
            .collect();
        assert_eq!(sessions.len(), 1, "exactly one page per session");
        assert_eq!(sessions[0].id, new_id);
    }

    #[test]
    fn synthesis_supersedes_legacy_raw_location_page() {
        let temp = tempfile::tempdir().expect("tempdir");
        let vault = temp.path();
        let external_id = "0a0a0a0a-1b1b-2c2c-3d3d-4e4e4e4e4e4e";

        // Simulate a pre-#926 raw page keyed on the archive-path location.
        let legacy_location = format!("session_transcripts/{external_id}.jsonl.gz");
        let legacy = SourceManifest::register_borrowed(
            vault,
            SourceDraftRef {
                location: legacy_location.clone(),
                kind: SourceKind::Session,
                fetched_at: "2026-06-01T00:00:00Z".to_string(),
                content: b"legacy raw transcript body",
                title: Some("Legacy raw".to_string()),
                citation: Some(legacy_location.clone()),
                license: None,
                ingestion_method: IngestionMethod::Manual,
                compile_status: CompileStatus::Pending,
            },
        )
        .expect("register legacy");
        assert_eq!(legacy.canonical_location, legacy_location);
        let legacy_derived =
            vault.join(derived_markdown_path(&legacy).expect("legacy derived path"));
        fs::create_dir_all(legacy_derived.parent().expect("parent")).expect("derived dir");
        fs::write(&legacy_derived, "# Legacy raw\n\nold verbose page").expect("legacy derived");
        assert!(legacy_derived.exists());

        // A daemon synthesis arrives for the same session.
        let wiki_dir = vault.join("session_wiki");
        fs::create_dir(&wiki_dir).expect("wiki dir");
        write_session_wiki(
            &wiki_dir,
            external_id,
            "## Summary\n\nReplaces the legacy page.\n",
        );

        let mut store = MemoryWikiStore::default();
        let report = sync_session_transcript_archives(
            vault,
            &mut store,
            &vault.join("missing-archives"),
            &wiki_dir,
            None,
            "2026-06-24T00:00:00Z",
        )
        .expect("sync");

        assert_eq!(report.accepted.len(), 1);
        assert!(!legacy_derived.exists(), "legacy derived page removed");
        let manifest = SourceManifest::read(vault).expect("manifest");
        let sessions: Vec<_> = manifest
            .entries
            .iter()
            .filter(|entry| entry.kind == SourceKind::Session)
            .collect();
        assert_eq!(sessions.len(), 1);
        assert_eq!(
            sessions[0].canonical_location,
            format!("session:{external_id}")
        );
    }
}

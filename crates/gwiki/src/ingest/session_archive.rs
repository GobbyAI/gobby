use std::collections::HashSet;
use std::fs::{self, File};
use std::io::{ErrorKind, Read};
use std::path::{Path, PathBuf};

use flate2::read::GzDecoder;

use crate::WikiError;
use crate::ingest::{IngestResult, index_after_ingest};
use crate::paths::{derived_markdown_path, raw_source_path};
use crate::sources::{SourceKind, SourceManifest, SourceRecord};
use crate::store::WikiIndexStore;

use super::session::{
    SessionFileSnapshot, SessionSummarizer, SessionWikiFileSnapshot,
    ingest_session_file_without_index, ingest_session_wiki_file_without_index,
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

/// A previously ingested session page whose source vanished from discovery and
/// was reconciled away (manifest entry + derived/raw files removed).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ReconciledSessionArchive {
    pub(crate) source_id: String,
    pub(crate) canonical_location: String,
    pub(crate) content_hash: String,
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
    pub(crate) reconciled: Vec<ReconciledSessionArchive>,
}

impl SessionArchiveBatchIngest {
    pub(crate) fn status(&self) -> &'static str {
        if !self.accepted.is_empty() {
            return if self.failed.is_empty() {
                "ingested"
            } else {
                "partial"
            };
        }
        if !self.failed.is_empty() {
            // No successful ingest. A reconcile/skip riding alongside failures is
            // a mixed (partial) outcome; pure failure stays "failed".
            return if self.reconciled.is_empty() && self.skipped.is_empty() {
                "failed"
            } else {
                "partial"
            };
        }
        if !self.reconciled.is_empty() {
            // Deletions reconciled with nothing newly ingested.
            return "reconciled";
        }
        if !self.skipped.is_empty() {
            return "skipped";
        }
        "empty"
    }

    /// True when the batch changed vault state — newly accepted sessions or
    /// reconciled deletions — so downstream index/Qdrant/Falkor sync must run.
    pub(crate) fn has_changes(&self) -> bool {
        !self.accepted.is_empty() || !self.reconciled.is_empty()
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

/// How to treat a raw transcript archive that has no daemon synthesis. Daemon
/// synthesis pages are always ingested regardless of this mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RawArchiveMode {
    /// Ignore raw archives (default): only daemon synthesis pages are ingested.
    Skip,
    /// Ingest a structural skeleton page from the raw transcript (`--raw`).
    Skeleton,
    /// Generate a daemon-equivalent summary via the shared handoff prompt,
    /// falling back to the skeleton when AI is unavailable (`--summarize`).
    Summarize,
}

pub(crate) fn sync_session_transcript_archives(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    archive_dir: &Path,
    wiki_dir: &Path,
    limit: Option<usize>,
    raw_mode: RawArchiveMode,
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
    let archive_paths = session_archive_paths(archive_dir)?;

    // Present session-source locations across the FULL pre-limit discovery.
    // Deletion reconciliation diffs the manifest against this set, so it must be
    // built before `limit` truncation — otherwise a bounded run would treat
    // every session beyond the cap as vanished and false-delete it.
    let present_locations = present_session_locations(&synthesized, &archive_paths);

    let mut work = synthesis_paths
        .into_iter()
        .map(SessionSourceFile::Synthesis)
        .collect::<Vec<_>>();
    work.extend(archive_paths.into_iter().map(SessionSourceFile::RawArchive));
    if let Some(limit) = limit {
        work.truncate(limit);
    }

    // One manifest snapshot drives dedup, supersede, and reconcile decisions for
    // the whole batch; removals below re-read under the manifest lock as needed.
    let manifest = SourceManifest::read(vault_root)?;
    let mut known_session_hashes = manifest
        .entries
        .iter()
        .filter(|entry| entry.kind == SourceKind::Session)
        .map(|entry| (entry.canonical_location.clone(), entry.content_hash.clone()))
        .collect::<HashSet<(String, String)>>();
    // Session pages already in the manifest. The `--summarize` path is gated on
    // page *existence* (not content hash) because LLM output is nondeterministic:
    // re-summarizing every run would superfluously regenerate and supersede. A
    // later daemon synthesis still supersedes a standalone page via the Synthesis
    // arm, which does not consult this set.
    let existing_session_pages = manifest
        .entries
        .iter()
        .filter(|entry| entry.kind == SourceKind::Session)
        .map(|entry| entry.canonical_location.clone())
        .collect::<HashSet<String>>();
    // Resolve AI once for the whole batch; `None` means standalone summarization
    // is unavailable (mode not Summarize, AI routed off, or no `ai` feature) and
    // raw archives fall back to skeleton pages.
    let summarizer = SessionSummarizer::resolve(raw_mode == RawArchiveMode::Summarize);
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
                // Dedup per canonical location, not globally: identical content
                // at different session locations must ingest independently.
                let canonical_location = format!("session:{external_id}");
                if known_session_hashes
                    .contains(&(canonical_location.clone(), content_hash.clone()))
                {
                    skipped.push(SkippedSessionArchive {
                        archive_path: path,
                        content_hash,
                        reason: "content_hash_already_ingested".to_string(),
                    });
                    continue;
                }
                let snapshot = SessionWikiFileSnapshot {
                    external_id: external_id.clone(),
                    path: path.clone(),
                    fetched_at: fetched_at.to_string(),
                    bytes,
                };
                match ingest_session_wiki_file_without_index(vault_root, snapshot) {
                    Ok(result) => {
                        let archive_path = path.clone();
                        let new_id = result.record.id.clone();
                        known_session_hashes.insert((
                            result.record.canonical_location.clone(),
                            result.record.content_hash.clone(),
                        ));
                        accepted.push(AcceptedSessionArchive {
                            archive_path: path,
                            result,
                        });
                        if let Err(error) =
                            supersede_session_page(vault_root, &manifest, &external_id, &new_id)
                        {
                            failed
                                .push(SessionArchiveFailure::from_wiki_error(&archive_path, error));
                        }
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
                // Only `Skip` mode ignores raw archives; both Skeleton and
                // Summarize process them (Summarize even without --raw).
                if raw_mode == RawArchiveMode::Skip {
                    skipped.push(SkippedSessionArchive {
                        archive_path: path,
                        content_hash: String::new(),
                        reason: "raw_fallback_disabled".to_string(),
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
                let file_name = archive_file_name(&path);
                // Key the raw fallback on the canonical `session:{external_id}` too,
                // so a later synthesis (or re-run) supersedes this page. `file_name`
                // still seeds the title and the `source_archive` provenance field.
                let external_id = external_id.unwrap_or_else(|| file_name.clone());
                let canonical_location = format!("session:{external_id}");

                // Standalone summary path: generate the daemon-format page once per
                // session. Gated on page existence (idempotent) because LLM output
                // is nondeterministic; a later daemon synthesis still supersedes it.
                if raw_mode == RawArchiveMode::Summarize {
                    if existing_session_pages.contains(&canonical_location) {
                        skipped.push(SkippedSessionArchive {
                            archive_path: path,
                            content_hash,
                            reason: "session_page_present".to_string(),
                        });
                        continue;
                    }
                    if let Some(summarizer) = &summarizer
                        && let Some(md_bytes) =
                            summarizer.summarize_archive(&path, &bytes, &external_id)
                    {
                        let snapshot = SessionWikiFileSnapshot {
                            external_id: external_id.clone(),
                            path: path.clone(),
                            fetched_at: fetched_at.to_string(),
                            bytes: md_bytes,
                        };
                        match ingest_session_wiki_file_without_index(vault_root, snapshot) {
                            Ok(result) => {
                                let archive_path = path.clone();
                                let new_id = result.record.id.clone();
                                known_session_hashes.insert((
                                    result.record.canonical_location.clone(),
                                    result.record.content_hash.clone(),
                                ));
                                accepted.push(AcceptedSessionArchive {
                                    archive_path: path,
                                    result,
                                });
                                if let Err(error) = supersede_session_page(
                                    vault_root,
                                    &manifest,
                                    &external_id,
                                    &new_id,
                                ) {
                                    failed.push(SessionArchiveFailure::from_wiki_error(
                                        &archive_path,
                                        error,
                                    ));
                                }
                            }
                            Err(error) => {
                                failed.push(SessionArchiveFailure::from_wiki_error(&path, error));
                            }
                        }
                        continue;
                    }
                    // Summary unavailable (AI off/empty/error): fall through to the
                    // skeleton below so the session still lands as a page.
                }

                // Skeleton page: the default --raw path, or the summarize fallback.
                // Dedup per canonical location keeps raw re-ingests idempotent.
                if known_session_hashes
                    .contains(&(canonical_location.clone(), content_hash.clone()))
                {
                    skipped.push(SkippedSessionArchive {
                        archive_path: path,
                        content_hash,
                        reason: "content_hash_already_ingested".to_string(),
                    });
                    continue;
                }
                let snapshot = SessionFileSnapshot {
                    location: canonical_location,
                    file_name,
                    fetched_at: fetched_at.to_string(),
                    path: path.clone(),
                    bytes,
                };
                match ingest_session_file_without_index(vault_root, snapshot) {
                    Ok(result) => {
                        let archive_path = path.clone();
                        let new_id = result.record.id.clone();
                        known_session_hashes.insert((
                            result.record.canonical_location.clone(),
                            result.record.content_hash.clone(),
                        ));
                        accepted.push(AcceptedSessionArchive {
                            archive_path: path,
                            result,
                        });
                        if let Err(error) =
                            supersede_session_page(vault_root, &manifest, &external_id, &new_id)
                        {
                            failed
                                .push(SessionArchiveFailure::from_wiki_error(&archive_path, error));
                        }
                    }
                    Err(error) => failed.push(SessionArchiveFailure::from_wiki_error(&path, error)),
                }
            }
        }
    }

    // Reconcile deletions: any manifest session whose source vanished from the
    // full discovery is removed here so the index cascade (IndexEvent::Deleted →
    // delete_derived_rows) prunes PostgreSQL rows, Qdrant points, and Falkor
    // nodes downstream. Driven by the pre-loop snapshot against the full
    // pre-limit present set so a `--limit` run never false-deletes uncapped
    // sessions. Entries supersede already removed return `None` here (no-op).
    let mut reconciled = Vec::new();
    if !present_locations.is_empty() {
        for entry in &manifest.entries {
            if entry.kind != SourceKind::Session
                || present_locations.contains(&entry.canonical_location)
            {
                continue;
            }
            if let Some(removed) = remove_session_page(vault_root, &entry.id)? {
                reconciled.push(ReconciledSessionArchive {
                    source_id: removed.id,
                    canonical_location: removed.canonical_location,
                    content_hash: removed.content_hash,
                });
            }
        }
    }

    let batch = SessionArchiveBatchIngest {
        archive_dir: archive_dir.to_path_buf(),
        scanned,
        accepted,
        skipped,
        failed,
        reconciled,
    };

    // Deletions alone still need downstream sync, so index on any change
    // (accepted or reconciled), not just newly accepted sessions.
    if batch.has_changes() {
        index_after_ingest(vault_root, store)?;
    }

    Ok(batch)
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

/// Canonical session-source locations for every discovered session, across the
/// full pre-limit synthesis + archive sets. Includes the canonical
/// `session:{external_id}` form, the legacy `session_transcripts/{id}.jsonl.gz`
/// location used before #926, and the `session:{file_name}` fallback for raw
/// archives whose external id cannot be stripped — so an entry is reconciled
/// away only when none of its possible location forms is present.
fn present_session_locations(
    synthesized: &HashSet<String>,
    archive_paths: &[PathBuf],
) -> HashSet<String> {
    let mut present = HashSet::new();
    for external_id in synthesized {
        insert_session_location_forms(&mut present, external_id);
    }
    for path in archive_paths {
        match session_external_id(path, SESSION_ARCHIVE_SUFFIX) {
            Some(external_id) => insert_session_location_forms(&mut present, &external_id),
            None => {
                present.insert(format!("session:{}", archive_file_name(path)));
            }
        }
    }
    present
}

fn insert_session_location_forms(present: &mut HashSet<String>, external_id: &str) {
    present.insert(format!("session:{external_id}"));
    present.insert(format!(
        "session_transcripts/{external_id}{SESSION_ARCHIVE_SUFFIX}"
    ));
}

/// Latest-wins replacement: remove any prior page for this session — under the
/// canonical `session:{external_id}` location or the legacy raw archive-path
/// location used before #926 — whose manifest id differs from the freshly
/// ingested record. Per-location dedup (issue A) lets a legacy raw entry share
/// the new entry's content hash, so supersede keys on id/location, never the
/// hash. Driven by the pre-loop snapshot, so it needs no extra manifest read.
/// Exactly one page survives per session.
fn supersede_session_page(
    vault_root: &Path,
    snapshot: &SourceManifest,
    external_id: &str,
    new_id: &str,
) -> Result<(), WikiError> {
    let canonical = format!("session:{external_id}");
    let legacy = format!("session_transcripts/{external_id}{SESSION_ARCHIVE_SUFFIX}");
    for entry in &snapshot.entries {
        if entry.kind == SourceKind::Session
            && entry.id != new_id
            && (entry.canonical_location == canonical || entry.canonical_location == legacy)
        {
            remove_session_page(vault_root, &entry.id)?;
        }
    }
    Ok(())
}

/// Removes a session page's manifest entry and its derived/raw files crash-
/// consistently: file cleanup runs first under the manifest lock, then the entry
/// is dropped and the manifest rewritten atomically. fs + manifest are not
/// transactional; an interrupted cleanup is retried on the next run because the
/// entry still points at the orphaned files. Returns the removed record, or
/// `None` if the id was already gone (e.g. supersede beat reconcile to it).
fn remove_session_page(vault_root: &Path, id: &str) -> Result<Option<SourceRecord>, WikiError> {
    SourceManifest::remove_with_cleanup(vault_root, id, |entry| {
        remove_vault_file(vault_root, &derived_markdown_path(entry)?)?;
        remove_vault_file(vault_root, &raw_source_path(&entry.id)?)?;
        Ok(())
    })
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
mod tests;

use std::io::Write;

use flate2::Compression;
use flate2::write::GzEncoder;

use super::*;
use crate::sources::{CompileStatus, IngestionMethod, SourceDraftRef};
use crate::store::MemoryWikiStore;
use crate::support::text::slugify_with_options;

mod summary;

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
        RawArchiveMode::Skeleton,
        "2026-06-16T20:05:00Z",
    )
    .expect("sync archives");

    assert_eq!(report.status(), "ingested");
    assert_eq!(report.scanned, 1);
    assert_eq!(report.accepted.len(), 1);
    assert!(report.skipped.is_empty());
    assert!(report.failed.is_empty());
    let raw_markdown = fs::read_to_string(temp.path().join(&report.accepted[0].result.raw_path))
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
        RawArchiveMode::Skip,
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
        RawArchiveMode::Skeleton,
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
        RawArchiveMode::Skeleton,
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
        RawArchiveMode::Skeleton,
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

fn source_id_for_test(canonical_location: &str, content_hash: &str) -> String {
    let hash_prefix = &content_hash[..content_hash.len().min(16)];
    let slug = slugify_with_options(canonical_location, None, Some(48));
    if slug.is_empty() {
        format!("src-{hash_prefix}")
    } else {
        format!("src-{hash_prefix}-{slug}")
    }
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
        RawArchiveMode::Skip,
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
        RawArchiveMode::Skeleton,
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
fn raw_archive_fallback_requires_opt_in_and_preserves_present_manifest_entry() {
    let temp = tempfile::tempdir().expect("tempdir");
    let vault = temp.path();
    let archive_dir = vault.join("session_transcripts");
    fs::create_dir(&archive_dir).expect("archive dir");
    let external_id = "22223333-4444-5555-6666-777788889999";
    let archive_path = archive_dir.join(format!("{external_id}.jsonl.gz"));
    fs::write(&archive_path, b"not gzip").expect("raw archive");

    let mut store = MemoryWikiStore::default();
    let report = sync_session_transcript_archives(
        vault,
        &mut store,
        &archive_dir,
        &vault.join("missing-session-wiki"),
        None,
        RawArchiveMode::Skip,
        "2026-06-24T00:05:00Z",
    )
    .expect("sync");

    assert_eq!(report.status(), "skipped");
    assert_eq!(report.scanned, 1);
    assert!(report.accepted.is_empty());
    assert_eq!(report.skipped.len(), 1);
    assert_eq!(report.skipped[0].archive_path, archive_path);
    assert_eq!(report.skipped[0].reason, "raw_fallback_disabled");
    assert!(report.failed.is_empty());

    let preserve_temp = tempfile::tempdir().expect("preserve tempdir");
    let preserve_vault = preserve_temp.path();
    let preserve_archive_dir = preserve_vault.join("session_transcripts");
    fs::create_dir(&preserve_archive_dir).expect("archive dir");
    let preserve_id = "99998888-7777-6666-5555-444433332222";
    write_archive(
        &preserve_archive_dir.join(format!("{preserve_id}.jsonl.gz")),
        br#"{"type":"session","timestamp":"2026-06-24T00:00:00Z","payload":{"title":"Raw preserve","messages":[{"role":"user","content":"Keep this raw fallback."}]}}"#,
    );

    let mut store = MemoryWikiStore::default();
    let initial = sync_session_transcript_archives(
        preserve_vault,
        &mut store,
        &preserve_archive_dir,
        &preserve_vault.join("missing-session-wiki"),
        None,
        RawArchiveMode::Skeleton,
        "2026-06-24T00:05:00Z",
    )
    .expect("initial sync");
    assert_eq!(initial.accepted.len(), 1);
    let record_id = initial.accepted[0].result.record.id.clone();

    let mut rerun_store = MemoryWikiStore::default();
    let rerun = sync_session_transcript_archives(
        preserve_vault,
        &mut rerun_store,
        &preserve_archive_dir,
        &preserve_vault.join("missing-session-wiki"),
        None,
        RawArchiveMode::Skip,
        "2026-06-24T00:06:00Z",
    )
    .expect("rerun sync");

    assert_eq!(rerun.status(), "skipped");
    assert!(rerun.accepted.is_empty());
    assert_eq!(rerun.skipped.len(), 1);
    assert_eq!(rerun.skipped[0].reason, "raw_fallback_disabled");
    assert!(rerun.failed.is_empty());
    assert!(rerun.reconciled.is_empty());
    let manifest = SourceManifest::read(preserve_vault).expect("manifest");
    let sessions: Vec<_> = manifest
        .entries
        .iter()
        .filter(|entry| entry.kind == SourceKind::Session)
        .collect();
    assert_eq!(sessions.len(), 1);
    assert_eq!(sessions[0].id, record_id);
    assert_eq!(
        sessions[0].canonical_location,
        format!("session:{preserve_id}")
    );
    assert!(
        preserve_vault
            .join("knowledge")
            .join("sources")
            .join(format!("{record_id}.md"))
            .exists(),
        "raw-backed manifest entry remains while archive is present"
    );
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
        RawArchiveMode::Skip,
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
        RawArchiveMode::Skip,
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
        RawArchiveMode::Skip,
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
fn failed_fresh_synthesis_preserves_previous_synthesis_page() {
    let temp = tempfile::tempdir().expect("tempdir");
    let vault = temp.path();
    let wiki_dir = vault.join("session_wiki");
    fs::create_dir(&wiki_dir).expect("wiki dir");
    let external_id = "fedcba00-1111-2222-3333-444455556666";

    write_session_wiki(&wiki_dir, external_id, "## Summary\n\nFirst revision.\n");
    let mut store = MemoryWikiStore::default();
    let first = sync_session_transcript_archives(
        vault,
        &mut store,
        &vault.join("missing-archives"),
        &wiki_dir,
        None,
        RawArchiveMode::Skip,
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

    let second_path =
        write_session_wiki(&wiki_dir, external_id, "## Summary\n\nSecond revision.\n");
    let second_bytes = fs::read(&second_path).expect("second wiki bytes");
    let second_hash = gobby_core::indexing::content_hash(&second_bytes);
    let new_id = source_id_for_test(&format!("session:{external_id}"), &second_hash);
    let blocking_raw = vault.join("raw").join(format!("{new_id}.md"));
    fs::create_dir(blocking_raw).expect("blocking raw dir");

    let mut store = MemoryWikiStore::default();
    let second = sync_session_transcript_archives(
        vault,
        &mut store,
        &vault.join("missing-archives"),
        &wiki_dir,
        None,
        RawArchiveMode::Skip,
        "2026-06-24T01:00:00Z",
    )
    .expect("second sync");

    assert_eq!(second.accepted.len(), 0);
    assert_eq!(second.failed.len(), 1);
    assert!(
        old_derived.exists(),
        "old derived page must survive failed replacement"
    );
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
    let legacy_derived = vault.join(derived_markdown_path(&legacy).expect("legacy derived path"));
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
        RawArchiveMode::Skip,
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

#[test]
fn vanished_session_source_is_reconciled_and_triggers_index() {
    let temp = tempfile::tempdir().expect("tempdir");
    let vault = temp.path();
    let wiki_dir = vault.join("session_wiki");
    fs::create_dir(&wiki_dir).expect("wiki dir");
    let external_id = "11111111-2222-3333-4444-555555555555";
    let survivor_id = "22222222-3333-4444-5555-666666666666";
    write_session_wiki(&wiki_dir, external_id, "## Summary\n\nWill vanish later.\n");
    write_session_wiki(&wiki_dir, survivor_id, "## Summary\n\nStays present.\n");

    // First sync ingests the synthesis into a persistent store.
    let mut store = MemoryWikiStore::default();
    let first = sync_session_transcript_archives(
        vault,
        &mut store,
        &vault.join("missing-archives"),
        &wiki_dir,
        None,
        RawArchiveMode::Skip,
        "2026-06-24T00:00:00Z",
    )
    .expect("first sync");
    assert_eq!(first.accepted.len(), 2);
    let record_id = first
        .accepted
        .iter()
        .find(|archive| {
            archive.result.record.canonical_location == format!("session:{external_id}")
        })
        .expect("vanishing session accepted")
        .result
        .record
        .id
        .clone();
    let derived = vault
        .join("knowledge")
        .join("sources")
        .join(format!("{record_id}.md"));
    assert!(derived.exists());
    assert!(indexed_store_text(&store).contains("Will vanish later"));
    assert!(indexed_store_text(&store).contains("Stays present"));

    // The session source disappears (daemon removed the synthesized page).
    fs::remove_file(wiki_dir.join(format!("{external_id}.md"))).expect("remove synthesis");

    let second = sync_session_transcript_archives(
        vault,
        &mut store,
        &vault.join("missing-archives"),
        &wiki_dir,
        None,
        RawArchiveMode::Skip,
        "2026-06-24T01:00:00Z",
    )
    .expect("second sync");

    assert!(second.accepted.is_empty());
    assert_eq!(second.reconciled.len(), 1);
    assert_eq!(second.reconciled[0].source_id, record_id);
    assert_eq!(
        second.reconciled[0].canonical_location,
        format!("session:{external_id}")
    );
    assert!(second.has_changes(), "reconcile-only run still has changes");
    assert_eq!(second.status(), "reconciled");

    // Page files removed and the deletion cascaded into the index.
    assert!(!derived.exists());
    assert!(!vault.join("raw").join(format!("{record_id}.md")).exists());
    let manifest = SourceManifest::read(vault).expect("manifest");
    let session_entries = manifest
        .entries
        .iter()
        .filter(|entry| entry.kind == SourceKind::Session)
        .collect::<Vec<_>>();
    assert_eq!(session_entries.len(), 1);
    assert_eq!(
        session_entries[0].canonical_location,
        format!("session:{survivor_id}")
    );
    assert!(
        store
            .deleted_paths
            .contains(&PathBuf::from(format!("knowledge/sources/{record_id}.md"))),
        "index cascade deletes the vanished derived page"
    );
    assert!(!indexed_store_text(&store).contains("Will vanish later"));
    assert!(indexed_store_text(&store).contains("Stays present"));
}

#[test]
fn limit_does_not_false_delete_uncapped_sessions() {
    let temp = tempfile::tempdir().expect("tempdir");
    let vault = temp.path();
    let wiki_dir = vault.join("session_wiki");
    fs::create_dir(&wiki_dir).expect("wiki dir");
    let first_id = "aaaaaaaa-0000-0000-0000-000000000001";
    let second_id = "bbbbbbbb-0000-0000-0000-000000000002";
    write_session_wiki(&wiki_dir, first_id, "## Summary\n\nFirst session.\n");
    write_session_wiki(&wiki_dir, second_id, "## Summary\n\nSecond session.\n");

    // Ingest both with no limit.
    let mut store = MemoryWikiStore::default();
    let initial = sync_session_transcript_archives(
        vault,
        &mut store,
        &vault.join("missing-archives"),
        &wiki_dir,
        None,
        RawArchiveMode::Skip,
        "2026-06-24T00:00:00Z",
    )
    .expect("initial sync");
    assert_eq!(initial.accepted.len(), 2);
    assert_eq!(session_entry_count(vault), 2);

    // Re-run with a cap of 1. The uncapped session must NOT be reconciled away,
    // because reconciliation diffs against the full pre-limit discovery.
    let mut store = MemoryWikiStore::default();
    let capped = sync_session_transcript_archives(
        vault,
        &mut store,
        &vault.join("missing-archives"),
        &wiki_dir,
        Some(1),
        RawArchiveMode::Skip,
        "2026-06-24T01:00:00Z",
    )
    .expect("capped sync");

    assert_eq!(capped.scanned, 1);
    assert!(
        capped.reconciled.is_empty(),
        "a --limit run must not false-delete"
    );
    assert_eq!(
        session_entry_count(vault),
        2,
        "both sessions survive a --limit run"
    );
}

#[test]
fn empty_discovery_preserves_existing_manifest_sessions() {
    let temp = tempfile::tempdir().expect("tempdir");
    let vault = temp.path();
    let archive_dir = vault.join("session_transcripts");
    fs::create_dir(&archive_dir).expect("archive dir");
    let external_id = "33333333-4444-5555-6666-777777777777";
    write_archive(
        &archive_dir.join(format!("{external_id}.jsonl.gz")),
        br#"{"type":"session","timestamp":"2026-06-24T00:00:00Z","payload":{"title":"Raw preserve","messages":[{"role":"user","content":"Keep me when discovery is empty."}]}}"#,
    );

    let mut store = MemoryWikiStore::default();
    let first = sync_session_transcript_archives(
        vault,
        &mut store,
        &archive_dir,
        &vault.join("missing-session-wiki"),
        None,
        RawArchiveMode::Skeleton,
        "2026-06-24T00:05:00Z",
    )
    .expect("initial sync");
    assert_eq!(first.accepted.len(), 1);
    let record_id = first.accepted[0].result.record.id.clone();
    fs::remove_dir_all(&archive_dir).expect("remove archive dir");

    let second = sync_session_transcript_archives(
        vault,
        &mut store,
        &vault.join("missing-session-transcripts"),
        &vault.join("missing-session-wiki"),
        None,
        RawArchiveMode::Skip,
        "2026-06-24T01:00:00Z",
    )
    .expect("empty discovery sync");

    assert_eq!(second.status(), "empty");
    assert!(second.reconciled.is_empty());
    let manifest = SourceManifest::read(vault).expect("manifest");
    assert!(manifest.entries.iter().any(|entry| {
        entry.kind == SourceKind::Session
            && entry.id == record_id
            && entry.canonical_location == format!("session:{external_id}")
    }));
    assert!(
        vault
            .join("knowledge")
            .join("sources")
            .join(format!("{record_id}.md"))
            .exists(),
        "session page remains when discovery finds no present locations"
    );
}

#[test]
fn same_content_at_two_session_locations_ingests_twice() {
    let temp = tempfile::tempdir().expect("tempdir");
    let vault = temp.path();
    let archive_dir = vault.join("session_transcripts");
    fs::create_dir(&archive_dir).expect("archive dir");
    let payload = br#"{"type":"session","timestamp":"2026-06-24T00:00:00Z","payload":{"title":"Shared body","messages":[{"role":"user","content":"Identical content."}]}}"#;
    write_archive(&archive_dir.join("session-one.jsonl.gz"), payload);
    write_archive(&archive_dir.join("session-two.jsonl.gz"), payload);

    let mut store = MemoryWikiStore::default();
    let report = sync_session_transcript_archives(
        vault,
        &mut store,
        &archive_dir,
        &vault.join("missing-session-wiki"),
        None,
        RawArchiveMode::Skeleton,
        "2026-06-24T00:05:00Z",
    )
    .expect("sync");

    // Per-location dedup: identical content at two locations ingests twice
    // (a global content-hash dedup would skip the second).
    assert_eq!(report.accepted.len(), 2);
    assert!(report.skipped.is_empty());
    let mut locations = report
        .accepted
        .iter()
        .map(|accepted| accepted.result.record.canonical_location.clone())
        .collect::<Vec<_>>();
    locations.sort();
    assert_eq!(
        locations,
        vec![
            "session:session-one".to_string(),
            "session:session-two".to_string(),
        ]
    );
}

#[test]
fn legacy_raw_entry_sharing_hash_is_superseded_by_id() {
    let temp = tempfile::tempdir().expect("tempdir");
    let vault = temp.path();
    let external_id = "cccccccc-1111-2222-3333-444455556666";
    let wiki_dir = vault.join("session_wiki");
    fs::create_dir(&wiki_dir).expect("wiki dir");
    let synthesis_path =
        write_session_wiki(&wiki_dir, external_id, "## Summary\n\nShared bytes.\n");
    let synthesis_bytes = fs::read(&synthesis_path).expect("synthesis bytes");

    // Pre-#926 legacy raw entry keyed on the archive-path location, sharing the
    // exact content hash of the incoming synthesis.
    let legacy_location = format!("session_transcripts/{external_id}.jsonl.gz");
    let legacy = SourceManifest::register_borrowed(
        vault,
        SourceDraftRef {
            location: legacy_location.clone(),
            kind: SourceKind::Session,
            fetched_at: "2026-06-01T00:00:00Z".to_string(),
            content: &synthesis_bytes,
            title: Some("Legacy raw".to_string()),
            citation: Some(legacy_location.clone()),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )
    .expect("register legacy");
    let legacy_derived = vault.join(derived_markdown_path(&legacy).expect("legacy derived path"));
    fs::create_dir_all(legacy_derived.parent().expect("parent")).expect("derived dir");
    fs::write(&legacy_derived, "# Legacy raw\n\nold page").expect("legacy derived");

    let mut store = MemoryWikiStore::default();
    let report = sync_session_transcript_archives(
        vault,
        &mut store,
        &vault.join("missing-archives"),
        &wiki_dir,
        None,
        RawArchiveMode::Skip,
        "2026-06-24T00:00:00Z",
    )
    .expect("sync");

    // Shared hash must not skip the new location, and supersede removes the
    // legacy entry by id/location rather than leaving it on a hash match.
    assert_eq!(report.accepted.len(), 1);
    assert!(report.skipped.is_empty());
    assert!(
        !legacy_derived.exists(),
        "legacy page superseded by id/location"
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
}

/// Count of `Session`-kind manifest entries in the vault.
fn session_entry_count(vault: &Path) -> usize {
    SourceManifest::read(vault)
        .expect("manifest")
        .entries
        .iter()
        .filter(|entry| entry.kind == SourceKind::Session)
        .count()
}

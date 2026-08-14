use super::*;

#[test]
fn standalone_summary_page_records_summary_mode_provenance() {
    // A standalone `--summarize` page is wrapped in the daemon `.md` format with
    // `summary_mode: standalone`; ingestion must surface it as
    // `session_summary_mode` so the vault can distinguish it from daemon synthesis.
    let temp = tempfile::tempdir().expect("tempdir");
    let bytes = b"---\ntitle: Refactor the indexer\nsource: claude\nmodel: claude-opus-4-8\ntags: []\nsession_id: sess-standalone\nsummary_mode: standalone\n---\n## Current State\nAll tests pass.\n".to_vec();
    let snapshot = SessionWikiFileSnapshot {
        external_id: "sess-standalone".to_string(),
        path: temp.path().join("session_wiki").join("sess-standalone.md"),
        fetched_at: "2026-06-25T00:00:00Z".to_string(),
        bytes,
    };

    let result = ingest_session_wiki_file_without_index(temp.path(), snapshot, None)
        .expect("ingest standalone");

    let derived = fs::read_to_string(
        temp.path()
            .join("knowledge")
            .join("sources")
            .join(format!("{}.md", result.record.id)),
    )
    .expect("derived markdown");
    assert!(
        derived.contains("session_summary_mode: standalone"),
        "expected standalone provenance, got: {derived}"
    );
    assert!(derived.contains("session_type: claude"));
    assert!(derived.contains("## Current State"));
    assert!(derived.contains("All tests pass."));
}

#[test]
fn summarize_skips_archives_that_already_have_a_session_page() {
    // `--summarize` is idempotent: once a session page exists it is left alone
    // (no regeneration, no AI call) because LLM output is nondeterministic.
    let temp = tempfile::tempdir().expect("tempdir");
    let archive_dir = temp.path().join("session_transcripts");
    fs::create_dir(&archive_dir).expect("archive dir");
    write_archive(
        &archive_dir.join("sess-idem.jsonl.gz"),
        br#"{"type":"session","timestamp":"2026-06-20T10:00:00Z","payload":{"title":"Idempotent","messages":[{"role":"user","content":"hello"}]}}"#,
    );
    let wiki_dir = temp.path().join("session_wiki");

    let mut store = FakeWikiStore::default();
    let first = sync_session_transcript_archives(
        temp.path(),
        &mut store,
        SessionArchiveSyncRequest {
            archive_dir: &archive_dir,
            wiki_dir: &wiki_dir,
            limit: None,
            raw_mode: RawArchiveMode::Skeleton,
            enrich: true,
            fetched_at: "2026-06-20T10:05:00Z",
        },
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("first sync");
    assert_eq!(first.accepted.len(), 1);

    let mut store = FakeWikiStore::default();
    let second = sync_session_transcript_archives(
        temp.path(),
        &mut store,
        SessionArchiveSyncRequest {
            archive_dir: &archive_dir,
            wiki_dir: &wiki_dir,
            limit: None,
            raw_mode: RawArchiveMode::Summarize,
            enrich: true,
            fetched_at: "2026-06-20T10:06:00Z",
        },
        &mut crate::progress::ProgressOptions::default(),
    )
    .expect("second sync");
    assert!(second.accepted.is_empty());
    assert!(
        second
            .skipped
            .iter()
            .any(|skip| skip.reason == "session_page_present"),
        "expected session_page_present skip, got: {:?}",
        second
            .skipped
            .iter()
            .map(|skip| skip.reason.as_str())
            .collect::<Vec<_>>()
    );
}

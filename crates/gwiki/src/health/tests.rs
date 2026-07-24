fn source_reference_is_present(markdown: &str, needle: &str) -> bool {
    let needle = needle.trim();
    if needle.is_empty() {
        return false;
    }
    let markdown = markdown_without_fenced_code(markdown);
    let matcher = AhoCorasick::new([needle]).expect("single pattern matcher builds");
    matcher
        .find_overlapping_iter(&markdown)
        .any(|matched| has_text_match_boundaries(&markdown, matched.start(), matched.end()))
}

use super::*;
use std::fs::FileTimes;
use std::time::{Duration, UNIX_EPOCH};

use crate::frontmatter::{WikiLifecycle, parse_frontmatter};
use crate::sources::{IngestionMethod, SourceDraft, SourceKind, SourceManifest};

#[test]
fn run_demotes_stale_detected_pages_to_stale_lifecycle() {
    let temp = tempfile::tempdir().expect("tempdir");
    write_page(
        temp.path(),
        "knowledge/concepts/aging.md",
        "---\ntitle: Aging\nlifecycle: verified\nstale_after: 2020-01-01\n---\n\nBody.\n",
    );
    write_page(
        temp.path(),
        "knowledge/concepts/fresh.md",
        "---\ntitle: Fresh\nlifecycle: verified\n---\n\nBody.\n",
    );

    let report = run(temp.path(), ScopeIdentity::project("proj")).expect("health run succeeds");
    assert_eq!(
        report.stale_pages,
        vec![PathBuf::from("knowledge/concepts/aging.md")]
    );

    let aging = std::fs::read_to_string(temp.path().join("knowledge/concepts/aging.md"))
        .expect("read aging page");
    let parsed = parse_frontmatter(&aging).expect("parse aging page");
    assert_eq!(parsed.metadata.lifecycle, Some(WikiLifecycle::Stale));
    assert!(parsed.metadata.unknown.contains_key("stale_at"));

    let fresh = std::fs::read_to_string(temp.path().join("knowledge/concepts/fresh.md"))
        .expect("read fresh page");
    let parsed_fresh = parse_frontmatter(&fresh).expect("parse fresh page");
    assert_eq!(
        parsed_fresh.metadata.lifecycle,
        Some(WikiLifecycle::Verified)
    );

    let log = std::fs::read_to_string(temp.path().join("log.md")).expect("read log");
    assert!(log.contains("lifecycle_transition:"), "{log}");
    assert!(log.contains("verified -> stale"), "{log}");

    // Re-running is idempotent: the page is already stale, no second
    // transition is logged.
    run(temp.path(), ScopeIdentity::project("proj")).expect("second health run");
    let log_after = std::fs::read_to_string(temp.path().join("log.md")).expect("re-read log");
    assert_eq!(
        log_after.matches("lifecycle_transition:").count(),
        1,
        "{log_after}"
    );
}

#[test]
fn inspect_does_not_demote_lifecycle() {
    let temp = tempfile::tempdir().expect("tempdir");
    let markdown =
        "---\ntitle: Aging\nlifecycle: verified\nstale_after: 2020-01-01\n---\n\nBody.\n";
    write_page(temp.path(), "knowledge/concepts/aging.md", markdown);

    inspect(temp.path(), ScopeIdentity::project("proj")).expect("inspect succeeds");

    let after = std::fs::read_to_string(temp.path().join("knowledge/concepts/aging.md"))
        .expect("read page");
    assert_eq!(after, markdown);
    assert!(!temp.path().join("log.md").exists());
}

#[test]
fn duplicate_sources_flags_rotated_hash_siblings_not_distinct_sources() {
    let temp = tempfile::tempdir().expect("tempdir");
    let sources = temp.path().join("knowledge/sources");
    std::fs::create_dir_all(&sources).expect("sources dir");

    let stub = |source_path: &str, body: &str| {
        format!(
            "---\ntitle: \"Example\"\nsource_kind: \"source_note\"\n\
                 synthesis_mode: \"source\"\nsource_path: \"{source_path}\"\n---\n\n\
                 # Example\n\n{body}\n"
        )
    };
    // Base + rotated-hash sibling for ONE canonical GitHub source: the
    // content hash rotated on re-fetch but the location slug is stable, so
    // both pages describe the same source and are orphaned duplicates.
    std::fs::write(
        sources.join("example-repo.md"),
        stub(
            "raw/src-0000000000000000-https-github-com-example-repo.md",
            "Base.",
        ),
    )
    .expect("base written");
    std::fs::write(
        sources.join("example-repo-2.md"),
        stub(
            "raw/src-1111111111111111-https-github-com-example-repo.md",
            "Sibling.",
        ),
    )
    .expect("sibling written");
    // A genuinely distinct source (different location) must NOT be flagged.
    std::fs::write(
        sources.join("other-repo.md"),
        stub(
            "raw/src-2222222222222222-https-github-com-other-repo.md",
            "Other.",
        ),
    )
    .expect("other written");
    // Two distinct notes captured from the same directory location share an
    // identity slug but differ by title — they must NOT be flagged as
    // duplicates of each other (the false positive an identity-only key hit).
    let titled_stub = |title: &str, source_path: &str| {
        format!(
            "---\ntitle: \"{title}\"\nsource_kind: \"source_note\"\n\
                 synthesis_mode: \"source\"\nsource_path: \"{source_path}\"\n---\n\n# {title}\n"
        )
    };
    std::fs::write(
        sources.join("note-alpha-md.md"),
        titled_stub(
            "note-alpha.md",
            "raw/src-3333333333333333-tmp-scratch-dir.md",
        ),
    )
    .expect("note alpha written");
    std::fs::write(
        sources.join("note-beta-md.md"),
        titled_stub(
            "note-beta.md",
            "raw/src-4444444444444444-tmp-scratch-dir.md",
        ),
    )
    .expect("note beta written");

    let pages = collect_pages(temp.path()).expect("pages collected");
    let duplicates = duplicate_sources(&pages);

    assert_eq!(duplicates.len(), 1, "{duplicates:?}");
    assert_eq!(duplicates[0].identity, "https-github-com-example-repo");
    assert_eq!(
        duplicates[0].paths,
        vec![
            PathBuf::from("knowledge/sources/example-repo-2.md"),
            PathBuf::from("knowledge/sources/example-repo.md"),
        ]
    );
}

#[test]
fn health_checks_required_cases() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let source = SourceManifest::register(
        root,
        SourceDraft::url(
            "https://example.com/uncited",
            "2026-05-29T12:00:00Z",
            "uncited source",
        )
        .with_citation("Uncited Example"),
    )
    .expect("source registered");
    write_page(
        root,
        "knowledge/topics/stale.md",
        "---\ntitle: Stale\nstale: true\n---\n# Stale\nSee [[Missing]].\n",
    );
    write_page(
        root,
        "knowledge/concepts/cache-a.md",
        "---\ntitle: Cache\nsource_kind: concept\n---\n# Cache\nConcept A.\n",
    );
    write_page(
        root,
        "knowledge/concepts/cache-b.md",
        "---\ntitle: Cache\nsource_kind: concept\n---\n# Cache\nConcept B.\n",
    );

    let report = run(root, ScopeIdentity::topic("ops")).expect("health runs");

    assert_eq!(
        report.stale_pages,
        vec![PathBuf::from("knowledge/topics/stale.md")]
    );
    assert_eq!(report.uncited_sources[0].source_id, source.id);
    assert_eq!(report.broken_links[0].target, "Missing");
    assert_eq!(report.duplicate_concepts[0].title, "Cache");
    assert_eq!(report.uncompiled_sources[0].source_id, source.id);
    assert!(root.join("meta/health/latest.json").exists());
    let markdown =
        std::fs::read_to_string(root.join("meta/health/latest.md")).expect("health markdown");
    assert!(markdown.starts_with("# Wiki health report\n\nScope: topic:ops\n"));
}

#[test]
fn duplicate_concepts_detect_alias_prefix_and_distinct_pairs() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    for (relative, title, aliases) in [
        ("knowledge/concepts/cache-a.md", "Cache", ""),
        ("knowledge/concepts/cache-b.md", "Cache", ""),
        (
            "knowledge/concepts/alpha.md",
            "Alpha",
            "aliases:\n  - Shared Concept\n",
        ),
        (
            "knowledge/concepts/beta.md",
            "Beta",
            "aliases:\n  - Shared Concept\n",
        ),
        ("knowledge/concepts/falkor.md", "Falkor", ""),
        ("knowledge/concepts/falkordb.md", "FalkorDB", ""),
        ("knowledge/concepts/session.md", "Session", ""),
        (
            "knowledge/concepts/session-manager.md",
            "SessionManager",
            "",
        ),
    ] {
        write_page(
            root,
            relative,
            &format!(
                "---\ntitle: {title}\nsource_kind: concept\n{aliases}---\n# {title}\n\nBody.\n"
            ),
        );
    }
    write_page(
        root,
        "meta/librarian/distinct-pairs.json",
        r#"{"pairs":[{"left":"knowledge/concepts/session.md","right":"knowledge/concepts/session-manager"}]}"#,
    );

    let report = inspect(root, ScopeIdentity::project("test")).expect("health inspection");

    assert!(report.duplicate_concepts.iter().any(|duplicate| {
        duplicate.reason == "exact_title"
            && duplicate.paths
                == vec![
                    PathBuf::from("knowledge/concepts/cache-a.md"),
                    PathBuf::from("knowledge/concepts/cache-b.md"),
                ]
    }));
    assert!(report.duplicate_concepts.iter().any(|duplicate| {
        duplicate.reason == "shared_key"
            && duplicate.paths
                == vec![
                    PathBuf::from("knowledge/concepts/alpha.md"),
                    PathBuf::from("knowledge/concepts/beta.md"),
                ]
    }));
    assert!(report.duplicate_concepts.iter().any(|duplicate| {
        duplicate.reason == "title_prefix"
            && duplicate.paths
                == vec![
                    PathBuf::from("knowledge/concepts/falkor.md"),
                    PathBuf::from("knowledge/concepts/falkordb.md"),
                ]
    }));
    assert!(!report.duplicate_concepts.iter().any(|duplicate| {
        duplicate
            .paths
            .iter()
            .any(|path| path == Path::new("knowledge/concepts/session.md"))
    }));

    let text = render_text(&report);
    assert!(text.contains("[exact_title]"));
    assert!(text.contains("[shared_key]"));
    assert!(text.contains("[title_prefix]"));
}

#[test]
fn export_health_reports_each_missing_artifact() {
    let temp = tempfile::tempdir().expect("tempdir");
    write_export_health_page(temp.path(), 100_000);

    let report = inspect(temp.path(), ScopeIdentity::project("proj")).expect("health");

    assert_eq!(
        export_issues(&report),
        vec![
            ("outputs/pages/", ExportArtifactStatus::Missing),
            ("outputs/graph.jsonld", ExportArtifactStatus::Missing),
            ("outputs/llms.txt", ExportArtifactStatus::Missing),
            ("outputs/llms-full.txt", ExportArtifactStatus::Missing),
        ],
    );
    let rendered = render_text(&report);
    assert!(rendered.contains("## Stale agent exports"));
    assert!(rendered.contains("- missing: outputs/pages/"));
}

#[test]
fn export_health_is_none_when_every_artifact_is_fresh() {
    let temp = tempfile::tempdir().expect("tempdir");
    write_export_health_page(temp.path(), 100_000);
    write_export_artifacts(temp.path(), [200_000; 4]);

    let report = inspect(temp.path(), ScopeIdentity::project("proj")).expect("health");

    assert_eq!(report.stale_exports, None);
}

#[test]
fn export_health_reports_artifacts_older_than_the_latest_page() {
    let temp = tempfile::tempdir().expect("tempdir");
    let artifact_time = 100_000;
    write_export_health_page(
        temp.path(),
        artifact_time + EXPORT_STALENESS_SLACK_SECONDS + 1,
    );
    write_export_artifacts(temp.path(), [artifact_time; 4]);

    let report = inspect(temp.path(), ScopeIdentity::project("proj")).expect("health");

    assert_eq!(
        export_issues(&report),
        vec![
            ("outputs/pages/", ExportArtifactStatus::Stale),
            ("outputs/graph.jsonld", ExportArtifactStatus::Stale),
            ("outputs/llms.txt", ExportArtifactStatus::Stale),
            ("outputs/llms-full.txt", ExportArtifactStatus::Stale),
        ],
    );
}

#[test]
fn export_health_keeps_partial_refreshes_independent() {
    let page_time = 200_000;
    let stale_time = page_time - EXPORT_STALENESS_SLACK_SECONDS - 1;

    let pages_fresh = tempfile::tempdir().expect("tempdir");
    write_export_health_page(pages_fresh.path(), page_time);
    write_export_artifacts(
        pages_fresh.path(),
        [page_time, stale_time, stale_time, stale_time],
    );
    let report =
        inspect(pages_fresh.path(), ScopeIdentity::project("pages-fresh")).expect("health");
    assert_eq!(
        export_issues(&report),
        vec![
            ("outputs/graph.jsonld", ExportArtifactStatus::Stale),
            ("outputs/llms.txt", ExportArtifactStatus::Stale),
            ("outputs/llms-full.txt", ExportArtifactStatus::Stale),
        ],
    );

    let graph_fresh = tempfile::tempdir().expect("tempdir");
    write_export_health_page(graph_fresh.path(), page_time);
    write_export_artifacts(
        graph_fresh.path(),
        [stale_time, page_time, page_time, page_time],
    );
    let report =
        inspect(graph_fresh.path(), ScopeIdentity::project("graph-fresh")).expect("health");
    assert_eq!(
        export_issues(&report),
        vec![("outputs/pages/", ExportArtifactStatus::Stale)]
    );
}

fn export_issues(report: &HealthReport) -> Vec<(&str, ExportArtifactStatus)> {
    report
        .stale_exports
        .as_ref()
        .expect("expected stale export findings")
        .iter()
        .map(|issue| (issue.artifact.as_str(), issue.status))
        .collect()
}

fn write_export_health_page(root: &Path, modified_seconds: u64) {
    let relative = "knowledge/concepts/export-health.md";
    write_page(
        root,
        relative,
        "---\ntitle: Export health\n---\n\nCurrent source page.\n",
    );
    set_modified(&root.join(relative), modified_seconds);
}

fn write_export_artifacts(root: &Path, modified_seconds: [u64; 4]) {
    for (relative, seconds) in [
        (
            "outputs/pages/knowledge/concepts/export-health.json",
            modified_seconds[0],
        ),
        ("outputs/graph.jsonld", modified_seconds[1]),
        ("outputs/llms.txt", modified_seconds[2]),
        ("outputs/llms-full.txt", modified_seconds[3]),
    ] {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().expect("artifact parent"))
            .expect("create artifact parent");
        std::fs::write(&path, "artifact").expect("write artifact");
        set_modified(&path, seconds);
    }
}

fn set_modified(path: &Path, seconds: u64) {
    let file = std::fs::OpenOptions::new()
        .write(true)
        .open(path)
        .expect("open timestamp target");
    file.set_times(FileTimes::new().set_modified(UNIX_EPOCH + Duration::from_secs(seconds)))
        .expect("set modified time");
}

#[test]
fn health_composes_page_confidence_for_knowledge_pages() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let source = SourceManifest::register(
        root,
        SourceDraft::url(
            "https://example.com/dispatch",
            Utc::now().to_rfc3339(),
            "dispatch source",
        )
        .with_citation("Dispatch Design Notes"),
    )
    .expect("source registered");
    write_page(
        root,
        "knowledge/concepts/dispatch.md",
        &format!(
            "---\ntitle: Dispatch\n---\n# Dispatch\nGrounded in [[knowledge/sources/{}]].\n",
            source.id
        ),
    );
    write_page(
        root,
        "knowledge/topics/automation.md",
        "---\ntitle: Automation\n---\n# Automation\nBuilt on [[Dispatch]].\n",
    );

    let report = run(root, ScopeIdentity::topic("ops")).expect("health runs");

    // Both knowledge synthesis pages are scored; freshly written pages
    // with a cited source and a backlink stay above the base score, so
    // nothing lands in the low-confidence list.
    let summary = &report.page_confidence;
    assert_eq!(summary.scored_pages, 2, "{summary:?}");
    let average = summary.average_score.expect("average confidence");
    assert!(
        average > 50,
        "fresh cited pages must average above the base score: {average}"
    );
    assert_eq!(summary.low_confidence_count, 0, "{summary:?}");
    assert!(summary.low_confidence.is_empty(), "{summary:?}");

    let markdown =
        std::fs::read_to_string(root.join("meta/health/latest.md")).expect("health markdown");
    assert!(markdown.contains("Page confidence:"), "{markdown}");
    assert!(markdown.contains("- scored pages: 2"), "{markdown}");
}

#[test]
fn inspect_does_not_persist_health_snapshots() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    SourceManifest::register(
        root,
        SourceDraft::url(
            "https://example.com/source",
            "2026-05-29T12:00:00Z",
            "source",
        )
        .with_citation("Example Source"),
    )
    .expect("source registered");
    write_page(
        root,
        "knowledge/topics/page.md",
        "# Page\nSee raw/INDEX.md.\n",
    );

    let report = inspect(root, ScopeIdentity::topic("ops")).expect("health inspects");

    assert_eq!(report.command, "health");
    assert!(!root.join("meta/health/latest.json").exists());
    assert!(!root.join("meta/health/latest.md").exists());
}

#[test]
fn source_reference_matching_skips_code_fences_and_partial_words() {
    assert!(!source_reference_is_present(
        "```md\nhttps://example.test/source\n```\n",
        "https://example.test/source"
    ));
    assert!(!source_reference_is_present(
        "prefixsource-idsuffix",
        "source-id"
    ));
    assert!(source_reference_is_present(
        "[Example](https://example.test/source)",
        "https://example.test/source"
    ));
}

#[test]
fn citation_index_marks_cited_sources_once_per_page() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let cited = SourceManifest::register(
        root,
        SourceDraft::url(
            "https://example.com/cited",
            "2026-05-29T12:00:00Z",
            "cited source",
        )
        .with_citation("Cited Example"),
    )
    .expect("cited source registered");
    let uncited = SourceManifest::register(
        root,
        SourceDraft::url(
            "https://example.com/uncited",
            "2026-05-29T12:00:00Z",
            "uncited source",
        )
        .with_citation("Uncited Example"),
    )
    .expect("uncited source registered");
    write_page(
        root,
        "knowledge/topics/cited.md",
        "# Cited\n\n[Cited Example](https://example.com/cited)\n",
    );

    let report = run(root, ScopeIdentity::topic("ops")).expect("health runs");
    let uncited_ids = report
        .uncited_sources
        .iter()
        .map(|issue| issue.source_id.as_str())
        .collect::<Vec<_>>();

    assert!(!uncited_ids.contains(&cited.id.as_str()));
    assert!(uncited_ids.contains(&uncited.id.as_str()));
}

#[test]
fn citation_index_survives_real_vault_scale() {
    // Regression: the citation regex set once exceeded the regex crate's
    // compiled-size limit at ~200 sources (each pattern repeating the
    // unicode boundary classes), silently degrading the index to
    // provenance-only and reporting every text-cited source as uncited.
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let mut cited_id = String::new();
    for index in 0..512 {
        let source = SourceManifest::register(
            root,
            SourceDraft::url(
                format!("session:0000{index:04}-4238-48bf-9edd-07ce27e3c481-{index:04}-long-id"),
                "2026-05-29T12:00:00Z",
                format!("session source {index}"),
            )
            .with_citation(format!("session:citation-{index:04}")),
        )
        .expect("source registered");
        if index == 150 {
            cited_id = source.id.clone();
        }
    }
    write_page(
        root,
        "recaps/2026-07-05.md",
        &format!(
            "---\ntitle: \"Recap: 2026-07-05\"\nrecap_date: 2026-07-05\n---\n# Recap\n\n\
                 ## Sessions\n\n- [[knowledge/sources/{cited_id}|Session]]\n"
        ),
    );

    let report = run(root, ScopeIdentity::topic("ops")).expect("health runs");

    let uncited_ids = report
        .uncited_sources
        .iter()
        .map(|issue| issue.source_id.as_str())
        .collect::<Vec<_>>();
    assert!(
        !uncited_ids.contains(&cited_id.as_str()),
        "recap link citation must count at 512-source scale"
    );
    assert_eq!(uncited_ids.len(), 511, "only the uncited 511 remain");
}

#[test]
fn recap_page_links_count_as_citations() {
    // recaps/ pages participate in the citation index (#17575): a source
    // whose only reference is its digest link on a daily recap page is
    // cited, not orphaned.
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let source = SourceManifest::register(
        root,
        SourceDraft::url(
            "https://example.com/session",
            "2026-05-29T12:00:00Z",
            "session source",
        )
        .with_citation("session:recap-only"),
    )
    .expect("source registered");
    write_page(
        root,
        "recaps/2026-07-05.md",
        &format!(
            "---\ntitle: \"Recap: 2026-07-05\"\nrecap_date: 2026-07-05\n---\n# Recap\n\n\
                 ## Sessions\n\n- [[knowledge/sources/{}|Session]]\n",
            source.id
        ),
    );

    let report = run(root, ScopeIdentity::topic("ops")).expect("health runs");

    let uncited_ids = report
        .uncited_sources
        .iter()
        .map(|issue| issue.source_id.as_str())
        .collect::<Vec<_>>();
    assert!(
        !uncited_ids.contains(&source.id.as_str()),
        "recap-linked source must count as cited: {uncited_ids:?}"
    );
}

#[test]
fn source_reference_matching_uses_unicode_word_boundaries() {
    assert!(!source_reference_is_present("αsource-id", "source-id"));
    assert!(!source_reference_is_present("source-idβ", "source-id"));
    assert!(!source_reference_is_present("source-id_", "source-id"));
    assert!(!source_reference_is_present("source-id9", "source-id"));
    assert!(source_reference_is_present("source-id.", "source-id"));
}

#[test]
fn stale_after_compares_dates_and_times_to_now() {
    let now = DateTime::parse_from_rfc3339("2026-06-02T12:00:00Z")
        .unwrap()
        .with_timezone(&Utc);

    assert!(stale_after_is_due("2026-06-02", now));
    assert!(stale_after_is_due("2026-06-02T11:59:59Z", now));
    assert!(!stale_after_is_due("2026-06-03", now));
    assert!(!stale_after_is_due("not-a-date", now));
}

#[test]
fn fenced_code_closes_only_on_matching_delimiter() {
    let markdown = "before\n~~~\nhttps://example.test/source\n```\nstill fenced\n~~~\nafter\n";

    let without_fences = markdown_without_fenced_code(markdown);

    assert_eq!(without_fences, "before\nafter\n");
}

#[test]
fn fenced_code_requires_matching_marker_length() {
    let markdown = "before\n````\nhttps://example.test/source\n```\nstill fenced\n````\nafter\n";

    let without_fences = markdown_without_fenced_code(markdown);

    assert_eq!(without_fences, "before\nafter\n");
}

#[test]
fn stale_citation_env_rejects_invalid_values() {
    assert_eq!(stale_citation_years_from_env("3"), Some(3));
    assert_eq!(stale_citation_years_from_env(" 2 "), Some(2));
    assert_eq!(stale_citation_years_from_env("0"), None);
    assert_eq!(stale_citation_years_from_env("nope"), None);
}

#[test]
fn stale_citation_uses_full_fetched_timestamp() {
    let now = DateTime::parse_from_rfc3339("2026-06-02T12:00:00Z")
        .unwrap()
        .with_timezone(&Utc);

    assert!(source_citation_is_stale_at(
        &source_record("2025-06-02T05:00:00Z"),
        now
    ));
    assert!(!source_citation_is_stale_at(
        &source_record("2025-06-02T18:00:00Z"),
        now
    ));
}

#[test]
fn change_triggered_refresh_health_degrades_to_provenance_only_mapping() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let mut provenance = ProvenanceGraph::default();
    provenance.add_link(crate::provenance::ProvenanceLink {
        source: crate::provenance::SourceChunkRef {
            source_id: "source-lib".to_string(),
            chunk_id: "source-lib#chunk-0".to_string(),
            path: PathBuf::from("src/lib.rs"),
            byte_start: 0,
            byte_end: 10,
        },
        section: crate::provenance::WikiSectionRef {
            page_path: PathBuf::from("code/lib.md"),
            heading: "Lib".to_string(),
            section_id: "lib".to_string(),
        },
        claim: None,
    });
    provenance.save_to_vault(root).expect("save provenance");

    let affected = change_triggered_affected_pages(
        root,
        None,
        "project-1",
        crate::code_graph::CodeChangeSet {
            files: vec!["src/lib.rs".to_string()],
            symbols: Vec::new(),
        },
    )
    .expect("affected pages");

    assert_eq!(affected.pages.len(), 1);
    assert_eq!(affected.pages[0].page_path, PathBuf::from("code/lib.md"));
    assert_eq!(affected.degradations.len(), 1);
}

fn write_page(root: &Path, relative: &str, markdown: &str) {
    let path = root.join(relative);
    std::fs::create_dir_all(path.parent().expect("page parent")).expect("create parent");
    std::fs::write(path, markdown).expect("write page");
}

fn source_record(fetched_at: &str) -> SourceRecord {
    SourceRecord {
        id: "source-id".to_string(),
        location: "https://example.test/source".to_string(),
        canonical_location: "https://example.test/source".to_string(),
        kind: SourceKind::Url,
        fetched_at: fetched_at.to_string(),
        last_verified_at: fetched_at.to_string(),
        fetch_provenance: crate::sources::FetchProvenance::Stub,
        content_hash: "hash".to_string(),
        title: None,
        citation: Some("Example".to_string()),
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Compiled,
        replay: None,
    }
}

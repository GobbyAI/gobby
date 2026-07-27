use std::time::{Duration, Instant};

use super::runner::{
    Cluster, HEALED_MENTIONS_PATH, PageDisposition, resolve_page_disposition, run_with_clock,
};
use super::*;
use crate::search::semantic::{SemanticSearchOutcome, SemanticSearchRequest};
use crate::search::{SearchHitKind, SearchProvenance, SearchScope, WikiSearchResult};
use crate::sources::{CompileStatus, IngestionMethod, SourceKind, SourceManifest, SourceRecord};

const TIMESTAMP: &str = "unix-ms:1750000000000";

fn scope() -> ScopeIdentity {
    ScopeIdentity::topic("upkeep-test")
}

fn research_scope(root: &Path) -> ResearchScope {
    ResearchScope::topic("upkeep-test", root)
}

fn pending_record(id: &str) -> SourceRecord {
    SourceRecord {
        id: id.to_string(),
        location: format!("{id}.md"),
        canonical_location: format!("canonical:{id}"),
        kind: SourceKind::Markdown,
        fetched_at: TIMESTAMP.to_string(),
        last_verified_at: TIMESTAMP.to_string(),
        fetch_provenance: crate::sources::FetchProvenance::Stub,
        content_hash: format!("{id}-hash"),
        title: Some(id.to_string()),
        citation: None,
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
        replay: None,
    }
}

fn write_file(root: &Path, relative: &str, content: &str) {
    let path = root.join(relative);
    fs::create_dir_all(path.parent().expect("parent")).expect("create parent");
    fs::write(path, content).expect("write file");
}

#[test]
fn archive_long_stale_pages_archives_only_aged_stale_pages() {
    use crate::frontmatter::{WikiLifecycle, parse_frontmatter};

    let temp = tempfile::tempdir().expect("tempdir");
    write_file(
        temp.path(),
        "knowledge/concepts/old-stale.md",
        "---\ntitle: Old\nlifecycle: stale\nstale_at: 2020-01-01T00:00:00Z\n---\n\nBody.\n",
    );
    let recent_stale_at = chrono::Utc::now().to_rfc3339();
    let recent = format!(
        "---\ntitle: Recent\nlifecycle: stale\nstale_at: {recent_stale_at}\n---\n\nBody.\n"
    );
    write_file(temp.path(), "knowledge/concepts/recent-stale.md", &recent);
    write_file(
        temp.path(),
        "knowledge/concepts/unstamped-stale.md",
        "---\ntitle: Unstamped\nlifecycle: stale\n---\n\nBody.\n",
    );

    let archived =
        archive_long_stale_pages(temp.path(), &scope(), &Options::default()).expect("archive pass");

    assert_eq!(
        archived,
        vec![PathBuf::from("knowledge/concepts/old-stale.md")]
    );
    let markdown = std::fs::read_to_string(temp.path().join("knowledge/concepts/old-stale.md"))
        .expect("read archived page");
    let parsed = parse_frontmatter(&markdown).expect("parse archived page");
    assert_eq!(parsed.metadata.lifecycle, Some(WikiLifecycle::Archived));
    assert!(parsed.metadata.unknown.contains_key("archived_at"));
    // The file stays at its stable path.
    assert!(temp.path().join("knowledge/concepts/old-stale.md").exists());

    let recent_after =
        std::fs::read_to_string(temp.path().join("knowledge/concepts/recent-stale.md"))
            .expect("read recent page");
    assert_eq!(recent_after, recent);

    let log = std::fs::read_to_string(temp.path().join("log.md")).expect("read log");
    assert_eq!(log.matches("lifecycle_transition:").count(), 1, "{log}");
    assert!(log.contains("stale -> archived"), "{log}");
}

#[test]
fn archive_long_stale_pages_dry_run_reports_without_writing() {
    let temp = tempfile::tempdir().expect("tempdir");
    let markdown =
        "---\ntitle: Old\nlifecycle: stale\nstale_at: 2020-01-01T00:00:00Z\n---\n\nBody.\n";
    write_file(temp.path(), "knowledge/concepts/old-stale.md", markdown);
    let options = Options {
        dry_run: true,
        ..Options::default()
    };

    let archived = archive_long_stale_pages(temp.path(), &scope(), &options).expect("dry-run pass");

    assert_eq!(
        archived,
        vec![PathBuf::from("knowledge/concepts/old-stale.md")]
    );
    let after = std::fs::read_to_string(temp.path().join("knowledge/concepts/old-stale.md"))
        .expect("read page");
    assert_eq!(after, markdown);
    assert!(!temp.path().join("log.md").exists());
}

#[test]
fn concept_worthiness_gates_candidates_before_clustering() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    for source in ["src-a", "src-b"] {
        seed_source(
            root,
            source,
            "See [[awk]], [[issue861]], [[task-16289]], [[fts5]], and [[FalkorDB]].\n",
        );
    }

    let report = run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("upkeep run");

    let targets = report
        .clusters
        .iter()
        .map(|cluster| cluster.target.as_str())
        .collect::<BTreeSet<_>>();
    assert_eq!(targets, BTreeSet::from(["FalkorDB", "fts5"]));
    for junk in ["awk", "issue861", "task-16289"] {
        assert!(
            !root.join(format!("knowledge/concepts/{junk}.md")).exists(),
            "junk concept `{junk}` was minted"
        );
    }
}

#[test]
fn concept_worthiness_archive_reports_are_stable_and_idempotent() {
    use crate::frontmatter::{WikiLifecycle, parse_frontmatter};

    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    for (relative, title) in [
        ("knowledge/concepts/_context.md", "_context"),
        ("knowledge/concepts/awk.md", "awk"),
        ("knowledge/concepts/task-16289.md", "task-16289"),
        ("knowledge/concepts/fts5.md", "fts5"),
        ("knowledge/concepts/bm25.md", "bm25"),
        ("knowledge/concepts/falkordb.md", "FalkorDB"),
        ("knowledge/concepts/agy.md", "AGY"),
    ] {
        write_file(
            root,
            relative,
            &format!("---\ntitle: {title}\nlifecycle: reviewed\n---\n\nBody.\n"),
        );
    }
    write_file(
        root,
        "knowledge/concepts/log.md",
        "---\ntitle: log\nlifecycle: draft\ncandidate: true\n---\n\nOrphaned candidate.\n",
    );
    // Generated folder contexts have no concept frontmatter. They share
    // this reserved filename and must remain catalog-owned.
    write_file(
        root,
        "knowledge/topics/_context.md",
        "# Topics\n\nGenerated folder context.\n",
    );

    let before = snapshot(root);
    let dry_run = run(
        research_scope(root),
        scope(),
        &Options {
            dry_run: true,
            ..Options::default()
        },
        None,
        None,
        TIMESTAMP,
    )
    .expect("dry-run upkeep");
    assert_eq!(snapshot(root), before, "dry run changed vault bytes");
    assert_eq!(
        dry_run.unworthy_archived,
        vec![
            UnworthyConceptArchive {
                page: PathBuf::from("knowledge/concepts/_context.md"),
                key: "_context".to_string(),
                reason: "leading_non_alphanumeric".to_string(),
            },
            UnworthyConceptArchive {
                page: PathBuf::from("knowledge/concepts/awk.md"),
                key: "awk".to_string(),
                reason: "generic_word".to_string(),
            },
            UnworthyConceptArchive {
                page: PathBuf::from("knowledge/concepts/log.md"),
                key: "log".to_string(),
                reason: "generic_word".to_string(),
            },
            UnworthyConceptArchive {
                page: PathBuf::from("knowledge/concepts/task-16289.md"),
                key: "task-16289".to_string(),
                reason: "artifact_id".to_string(),
            },
        ]
    );
    let dry_text = render_text(&dry_run);
    assert!(
        dry_text.contains("- knowledge/concepts/_context.md [_context]: leading_non_alphanumeric"),
        "{dry_text}"
    );

    let applied = run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("applied upkeep");
    assert_eq!(applied.unworthy_archived, dry_run.unworthy_archived);

    for relative in [
        "knowledge/concepts/awk.md",
        "knowledge/concepts/task-16289.md",
    ] {
        let markdown = fs::read_to_string(root.join(relative)).expect("archived page");
        let parsed = parse_frontmatter(&markdown).expect("archived frontmatter");
        assert_eq!(parsed.metadata.lifecycle, Some(WikiLifecycle::Archived));
    }
    let log = fs::read_to_string(root.join("log.md")).expect("upkeep log");
    assert!(
            log.contains(
                "knowledge/concepts/_context.md: reviewed -> archived (upkeep: unworthy concept key `_context` (leading_non_alphanumeric))"
            ),
            "{log}"
        );
    for relative in [
        "knowledge/concepts/fts5.md",
        "knowledge/concepts/bm25.md",
        "knowledge/concepts/falkordb.md",
        "knowledge/concepts/agy.md",
    ] {
        let markdown = fs::read_to_string(root.join(relative)).expect("active page");
        let parsed = parse_frontmatter(&markdown).expect("active frontmatter");
        assert_eq!(parsed.metadata.lifecycle, Some(WikiLifecycle::Reviewed));
    }
    let index = fs::read_to_string(root.join("knowledge/INDEX.md")).expect("knowledge index");
    assert!(!index.contains("concepts/awk"), "{index}");
    assert!(!index.contains("concepts/task-16289"), "{index}");
    assert!(index.contains("concepts/agy"), "{index}");

    let rerun = run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("second upkeep");
    assert!(
        rerun.unworthy_archived.is_empty(),
        "{:?}",
        rerun.unworthy_archived
    );
}

/// Register a pending source: manifest entry, raw body, and digest page.
fn seed_source(root: &Path, id: &str, digest_body: &str) {
    let record = pending_record(id);
    write_file(
        root,
        &format!("raw/{id}.md"),
        &format!("# {id}\n\nRaw source body for {id}.\n"),
    );
    write_file(root, &format!("knowledge/sources/{id}.md"), digest_body);
    SourceManifest::update(root, |manifest| {
        manifest.entries.push(record.clone());
        Ok(true)
    })
    .expect("seed manifest entry");
}

fn compile_status_of(root: &Path, id: &str) -> CompileStatus {
    SourceManifest::read(root)
        .expect("read manifest")
        .entries
        .iter()
        .find(|entry| entry.id == id)
        .unwrap_or_else(|| panic!("manifest entry {id}"))
        .compile_status
        .clone()
}

fn snapshot(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    let mut files = BTreeMap::new();
    snapshot_into(root, root, &mut files);
    files
}

fn snapshot_into(root: &Path, directory: &Path, files: &mut BTreeMap<PathBuf, Vec<u8>>) {
    for entry in fs::read_dir(directory).expect("read dir") {
        let entry = entry.expect("dir entry");
        let path = entry.path();
        if path.is_dir() {
            snapshot_into(root, &path, files);
        } else {
            files.insert(
                path.strip_prefix(root).expect("relative").to_path_buf(),
                fs::read(&path).expect("read file"),
            );
        }
    }
}

struct FixedSemanticBackend {
    hits: Vec<WikiSearchResult>,
}

impl SemanticSearchBackend for FixedSemanticBackend {
    fn search_semantic(
        &mut self,
        _request: SemanticSearchRequest,
    ) -> Result<SemanticSearchOutcome, crate::search::SearchError> {
        Ok(SemanticSearchOutcome {
            hits: self.hits.clone(),
            degradation: None,
        })
    }
}

fn semantic_hit(path: &str, score: f64) -> WikiSearchResult {
    WikiSearchResult {
        id: path.to_string(),
        title: None,
        scope: SearchScope::topic("upkeep-test"),
        path: PathBuf::from(path),
        source_path: PathBuf::from(path),
        hit_kind: SearchHitKind::Document,
        snippet: String::new(),
        score,
        sources: Vec::new(),
        explanations: Vec::new(),
        chunk: None,
        provenance: SearchProvenance {
            document_path: PathBuf::from(path),
            source_path: PathBuf::from(path),
            source_kind: "document".to_string(),
            content_hash: None,
        },
    }
}

#[test]
fn upkeep_drains_case_variant_cluster_into_one_entity_concept_page() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(root, "src-a", "Uses [[gcode]] for symbol search.\n");
    seed_source(
        root,
        "src-b",
        "Prefers [[Gcode]]; still [[gcode]] underneath.\n",
    );

    let report = run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("upkeep run");

    assert_eq!(report.pending_before, 2);
    assert_eq!(report.pending_after, 0);
    assert_eq!(report.pages_created, 1);
    assert_eq!(report.clusters.len(), 1);
    let cluster = &report.clusters[0];
    assert_eq!(cluster.action, "created");
    assert_eq!(cluster.target, "gcode");
    assert_eq!(cluster.variants, vec!["gcode", "Gcode"]);
    assert_eq!(cluster.mentions, 3);
    assert_eq!(cluster.source_ids, vec!["src-a", "src-b"]);
    assert_eq!(
        cluster.page_path.as_deref(),
        Some(Path::new("knowledge/concepts/gcode.md"))
    );

    let page =
        fs::read_to_string(root.join("knowledge/concepts/gcode.md")).expect("concept page written");
    assert!(page.contains("Gcode"), "aliases carry observed variants");
    assert!(page.contains("entity"), "entity tag rendered: {page}");
    assert!(
        page.contains("knowledge/sources/src-a") && page.contains("knowledge/sources/src-b"),
        "provenance links point at the source digests: {page}"
    );

    assert_eq!(compile_status_of(root, "src-a"), CompileStatus::Compiled);
    assert_eq!(compile_status_of(root, "src-b"), CompileStatus::Compiled);

    let index = fs::read_to_string(root.join("_index.md")).expect("index regenerated");
    assert!(index.contains("knowledge/concepts/gcode"), "{index}");

    let log = fs::read_to_string(root.join("log.md")).expect("log written");
    assert!(log.contains("page_created:"), "{log}");
    assert!(log.contains("upkeep_completed:"), "{log}");

    assert!(root.join(REPORT_RELATIVE_PATH).exists());
    assert!(
        report
            .notes
            .iter()
            .any(|note| note.contains("semantic backend unavailable")),
        "missing backend is noted: {:?}",
        report.notes
    );
    assert!(
        root.join(crate::vault::STATE_ROOT)
            .join("research-session.json")
            .try_exists()
            .is_ok_and(|exists| !exists),
        "upkeep must not persist a research checkpoint"
    );
}

#[test]
fn upkeep_created_concept_pages_enter_candidate_quarantine() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(root, "src-a", "Uses [[gcode]] for symbol search.\n");
    seed_source(root, "src-b", "Prefers [[gcode]] everywhere.\n");

    let report = run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("upkeep run");

    let page =
        fs::read_to_string(root.join("knowledge/concepts/gcode.md")).expect("concept page written");
    assert!(
        page.contains("lifecycle: draft") && page.contains("candidate: true"),
        "created concept page starts quarantined: {page}"
    );

    let log = fs::read_to_string(root.join("log.md")).expect("log written");
    assert!(log.contains("candidate_proposed:"), "{log}");

    // Digest backlinks gated the page's creation; they are not
    // corroboration, so the fresh candidate is neither promoted nor
    // discarded by the same run.
    assert!(report.candidates_promoted.is_empty());
    assert!(report.candidates_discarded.is_empty());
}

#[test]
fn govern_candidates_promotes_candidates_with_knowledge_backlinks() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    write_file(
        root,
        "knowledge/concepts/widget.md",
        "---\ntitle: Widget\nlifecycle: draft\ncandidate: true\n---\n\n# Widget\n\nBody.\n",
    );
    write_file(
        root,
        "knowledge/concepts/gadget.md",
        "---\ntitle: Gadget\n---\n\nPairs with [[Widget]].\n",
    );
    write_file(
        root,
        "knowledge/topics/assembly.md",
        "---\ntitle: Assembly\n---\n\nStarts from a [[Widget]].\n",
    );

    let (promoted, discarded) =
        govern_candidates(root, &scope(), TIMESTAMP).expect("governance pass");

    assert_eq!(
        promoted,
        vec![PathBuf::from("knowledge/concepts/widget.md")]
    );
    assert!(discarded.is_empty());
    let page = fs::read_to_string(root.join("knowledge/concepts/widget.md")).expect("read page");
    assert!(!page.contains("candidate: true"), "{page}");
    let log = fs::read_to_string(root.join("log.md")).expect("log written");
    assert!(log.contains("candidate_promoted:"), "{log}");
}

#[test]
fn govern_candidates_discards_orphans_and_keeps_digest_backed_candidates() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    write_file(
        root,
        "knowledge/concepts/orphan.md",
        "---\ntitle: Orphan\nlifecycle: draft\ncandidate: true\n---\n\n# Orphan\n\nBody.\n",
    );
    write_file(
        root,
        "knowledge/concepts/mentioned.md",
        "---\ntitle: Mentioned\nlifecycle: draft\ncandidate: true\n---\n\n# Mentioned\n\nBody.\n",
    );
    write_file(
        root,
        "knowledge/sources/digest.md",
        "---\ntitle: Digest\n---\n\nStill cites [[Mentioned]].\n",
    );

    let (promoted, discarded) =
        govern_candidates(root, &scope(), TIMESTAMP).expect("governance pass");

    assert!(promoted.is_empty());
    assert_eq!(
        discarded,
        vec![PathBuf::from("knowledge/concepts/orphan.md")]
    );

    // The orphan is archived in place; the digest-backed candidate stays
    // quarantined awaiting knowledge-page corroboration.
    let orphan =
        fs::read_to_string(root.join("knowledge/concepts/orphan.md")).expect("orphan page");
    assert!(orphan.contains("lifecycle: archived"), "{orphan}");
    let mentioned =
        fs::read_to_string(root.join("knowledge/concepts/mentioned.md")).expect("mentioned page");
    assert!(mentioned.contains("candidate: true"), "{mentioned}");

    let log = fs::read_to_string(root.join("log.md")).expect("log written");
    assert!(log.contains("candidate_discarded:"), "{log}");
    assert!(log.contains("lifecycle_transition:"), "{log}");
}

#[test]
fn upkeep_clusters_targets_mentioned_only_by_compiled_digests() {
    // Regression: convergence must not depend on drain state. A target
    // whose mentioning digests were already reconciled (by a run whose
    // generation failed, or by consumption in another cluster) still gets
    // its entity page; only the drain bookkeeping is pending-scoped.
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(root, "src-a", "Runs on [[PostgreSQL]].\n");
    seed_source(root, "src-b", "Migrated to [[PostgreSQL]] storage.\n");
    SourceManifest::update(root, |manifest| {
        for entry in &mut manifest.entries {
            entry.compile_status = CompileStatus::Compiled;
        }
        Ok(true)
    })
    .expect("mark sources compiled");

    let report = run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("upkeep run");

    assert_eq!(report.pending_before, 0);
    assert_eq!(report.pending_after, 0);
    assert!(
        report.reconciled_no_synthesis.is_empty(),
        "compiled digests feeding a cluster are evidence reuse, not a drain change"
    );
    assert_eq!(report.pages_created, 1);
    assert_eq!(report.clusters.len(), 1);
    let cluster = &report.clusters[0];
    assert_eq!(cluster.target, "PostgreSQL");
    assert_eq!(cluster.source_ids, vec!["src-a", "src-b"]);
    assert_eq!(
        cluster.page_path.as_deref(),
        Some(Path::new("knowledge/concepts/postgresql.md"))
    );
}

#[test]
fn upkeep_skips_path_shaped_targets_while_entity_targets_still_cluster() {
    // Path-shaped targets ([[code/files/foo.md]], [[knowledge/sources/...]])
    // must not form clusters: slugify would flatten '/' into '-' and mint
    // junk pages like knowledge/concepts/code-files-foo-md.md (#17652).
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(
        root,
        "src-a",
        "See [[code/files/foo.md]] and [[FalkorDB]].\n",
    );
    seed_source(
        root,
        "src-b",
        "Also [[code/files/foo.md]] plus [[FalkorDB]].\n",
    );
    // Mentions only a digest-shaped target with no live manifest record:
    // never clusters, reconciles as reviewed-no-synthesis.
    seed_source(root, "src-c", "Digest link [[knowledge/sources/src-zz]].\n");

    let report = run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("upkeep run");

    assert_eq!(
        report.clusters.len(),
        1,
        "path-shaped targets must not form clusters"
    );
    let cluster = &report.clusters[0];
    assert_eq!(cluster.target, "FalkorDB");
    assert_eq!(cluster.source_ids, vec!["src-a", "src-b"]);
    assert_eq!(report.pages_created, 1);
    assert!(
        !root
            .join("knowledge/concepts/code-files-foo-md.md")
            .exists(),
        "path-shaped target must not mint a junk entity page"
    );
    assert!(
        !root
            .join("knowledge/concepts/knowledge-sources-src-zz.md")
            .exists(),
        "digest-shaped target must not mint a junk entity page"
    );
    assert!(root.join("knowledge/concepts/falkordb.md").exists());
    assert_eq!(
        report.reconciled_no_synthesis,
        vec!["src-c"],
        "a digest mentioning only path-shaped targets reconciles as reviewed-no-synthesis"
    );
}

#[test]
fn upkeep_dry_run_leaves_vault_bytes_unchanged() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(root, "src-a", "Mentions [[gcode]].\n");
    seed_source(root, "src-b", "Mentions [[Gcode]] again.\n");
    let before = snapshot(root);

    let report = run(
        research_scope(root),
        scope(),
        &Options {
            dry_run: true,
            ..Options::default()
        },
        None,
        None,
        TIMESTAMP,
    )
    .expect("dry run");

    assert_eq!(snapshot(root), before, "dry run must not write vault bytes");
    assert_eq!(report.clusters.len(), 1);
    assert_eq!(report.clusters[0].action, "planned_create");
    assert_eq!(report.pending_after, report.pending_before);
    assert!(report.reconciled_no_synthesis.is_empty());
}

#[test]
fn upkeep_skips_sources_with_invalid_digest_paths() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(root, "src-a", "No entity mentions here.\n");
    SourceManifest::update(root, |manifest| {
        manifest.entries.push(pending_record("../bad"));
        Ok(true)
    })
    .expect("seed invalid manifest entry");

    let report = run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("upkeep run skips bad digest path");

    assert!(
        report
            .notes
            .iter()
            .any(|note| note.contains("skipping source `../bad`")),
        "{:?}",
        report.notes
    );
}

#[test]
fn upkeep_migrates_reserved_instruction_filename_pages() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(root, "src-a", "No entity mentions here.\n");
    write_file(
        root,
        "knowledge/concepts/claude.md",
        "---\ntitle: \"Claude\"\n---\n\n# Claude\n\nBody.\n",
    );
    // A page the catalog does not regenerate, holding both link forms.
    write_file(
        root,
        "knowledge/topics/tour.md",
        "---\ntitle: \"Tour\"\n---\n\nSee [[knowledge/concepts/claude|Claude]] and \
             [Claude](knowledge/concepts/claude.md).\n",
    );

    let report = run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("upkeep run");

    assert!(
        !root.join("knowledge/concepts/claude.md").exists(),
        "reserved filename must be renamed"
    );
    assert!(root.join("knowledge/concepts/claude-concept.md").exists());
    let migrated = fs::read_to_string(root.join("knowledge/concepts/claude-concept.md"))
        .expect("migrated page");
    assert!(migrated.contains("title: \"Claude\""), "{migrated}");
    let tour = fs::read_to_string(root.join("knowledge/topics/tour.md")).expect("tour");
    assert!(
        tour.contains("[[knowledge/concepts/claude-concept|Claude]]"),
        "{tour}"
    );
    assert!(
        tour.contains("[Claude](knowledge/concepts/claude-concept.md)"),
        "{tour}"
    );
    let index = fs::read_to_string(root.join("knowledge/INDEX.md")).expect("index");
    assert!(
        index.contains("knowledge/concepts/claude-concept"),
        "{index}"
    );
    assert!(
        report
            .notes
            .iter()
            .any(|note| note.contains("claude-concept")),
        "{:?}",
        report.notes
    );
}

#[test]
fn upkeep_reserved_filename_migration_claims_batch_destinations() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(root, "src-a", "No entity mentions here.\n");
    write_file(
        root,
        "knowledge/concepts/claude.md",
        "---\ntitle: \"Claude\"\n---\n\n# Claude\n",
    );
    write_file(
        root,
        "knowledge/concepts/claude-concept.md",
        "---\ntitle: \"Existing Claude Concept\"\n---\n\n# Existing Claude Concept\n",
    );
    write_file(
        root,
        "knowledge/concepts/agents.md",
        "---\ntitle: \"Agents\"\n---\n\n# Agents\n",
    );

    run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("upkeep run");

    assert!(root.join("knowledge/concepts/claude-concept.md").exists());
    assert!(root.join("knowledge/concepts/claude-concept-2.md").exists());
    assert!(root.join("knowledge/concepts/agents-concept.md").exists());
    assert!(!root.join("knowledge/concepts/claude.md").exists());
    assert!(!root.join("knowledge/concepts/agents.md").exists());
    let migrated = fs::read_to_string(root.join("knowledge/concepts/claude-concept-2.md"))
        .expect("migrated claude page");
    assert!(migrated.contains("title: \"Claude\""), "{migrated}");
    let existing = fs::read_to_string(root.join("knowledge/concepts/claude-concept.md"))
        .expect("existing claude concept page");
    assert!(existing.contains("Existing Claude Concept"), "{existing}");
}

#[test]
fn upkeep_dry_run_only_reports_reserved_filename_migration() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(root, "src-a", "No entity mentions here.\n");
    write_file(
        root,
        "knowledge/concepts/gemini.md",
        "---\ntitle: \"Gemini\"\n---\n\n# Gemini\n\nBody.\n",
    );
    let before = snapshot(root);

    let report = run(
        research_scope(root),
        scope(),
        &Options {
            dry_run: true,
            ..Options::default()
        },
        None,
        None,
        TIMESTAMP,
    )
    .expect("dry run");

    assert_eq!(snapshot(root), before, "dry run must not write vault bytes");
    assert!(
        report
            .notes
            .iter()
            .any(|note| note.contains("would rename") && note.contains("gemini")),
        "{:?}",
        report.notes
    );
}

#[test]
fn upkeep_leaves_alias_resolved_mentions_to_the_existing_entity_page() {
    // Case-insensitive link resolution (stem/title/alias) already binds
    // [[GCode]]/[[gcode]] to the existing aliased page, so no cluster
    // forms, no duplicate concept page is created, and the sources
    // reconcile as reviewed-without-synthesis.
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(root, "src-a", "About [[GCode]].\n");
    seed_source(root, "src-b", "More on [[gcode]].\n");
    write_file(
        root,
        "knowledge/concepts/code-index.md",
        "---\ntitle: Code Index\naliases:\n  - Gcode\n---\n\n# Code Index\n\nExisting body.\n",
    );

    let report = run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("upkeep run");

    assert!(report.clusters.is_empty());
    assert_eq!(report.pages_created, 0);
    assert_eq!(report.pages_updated, 0);
    assert!(
        !root.join("knowledge/concepts/gcode.md").exists(),
        "no duplicate page created for an already-covered entity"
    );
    assert_eq!(
        report.reconciled_no_synthesis,
        vec!["src-a".to_string(), "src-b".to_string()]
    );
    assert_eq!(compile_status_of(root, "src-a"), CompileStatus::Compiled);
    assert_eq!(compile_status_of(root, "src-b"), CompileStatus::Compiled);
}

#[test]
fn exact_alias_match_layer_updates_without_touching_the_semantic_probe() {
    // Defense-in-depth for the first update-over-create layer: if a
    // cluster key ever reaches disposition while an existing page carries
    // that key, upkeep updates the page and skips the near-dup probe.
    let cluster = Cluster {
        key: "gcode".to_string(),
        primary: "Gcode".to_string(),
        variants: vec!["Gcode".to_string()],
        mentions: 2,
        source_indices: Vec::new(),
    };
    let existing = vec![(
        PathBuf::from("knowledge/concepts/code-index.md"),
        BTreeSet::from(["code index".to_string(), "gcode".to_string()]),
    )];
    let mut semantic: Option<SemanticProbe<'_>> = None;

    match resolve_page_disposition(&cluster, &existing, &mut semantic) {
        PageDisposition::Update {
            page,
            near_duplicate,
        } => {
            assert_eq!(page, PathBuf::from("knowledge/concepts/code-index.md"));
            assert!(near_duplicate.is_none());
        }
        PageDisposition::Create { .. } => panic!("exact match must update, not create"),
    }
}

#[test]
fn upkeep_near_duplicate_hit_chooses_update_over_create() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(root, "src-a", "The [[Gobby Daemon]] never sleeps.\n");
    seed_source(root, "src-b", "Restart the [[gobby daemon]] nightly.\n");
    write_file(
        root,
        "knowledge/concepts/long-running-service.md",
        "---\ntitle: Long Running Service\n---\n\n# Long Running Service\n\nBody.\n",
    );

    let mut backend = FixedSemanticBackend {
        hits: vec![semantic_hit(
            "knowledge/concepts/long-running-service.md",
            0.95,
        )],
    };
    let probe = SemanticProbe {
        backend: &mut backend,
        search_scope: SearchScope::topic("upkeep-test"),
    };
    let report = run(
        research_scope(root),
        scope(),
        &Options::default(),
        Some(probe),
        None,
        TIMESTAMP,
    )
    .expect("upkeep run");

    let cluster = &report.clusters[0];
    assert_eq!(cluster.action, "updated");
    assert_eq!(
        cluster.page_path.as_deref(),
        Some(Path::new("knowledge/concepts/long-running-service.md"))
    );
    let near = cluster.near_duplicate.as_ref().expect("near-dup recorded");
    assert_eq!(
        near.page,
        PathBuf::from("knowledge/concepts/long-running-service.md")
    );
    assert!((near.score - 0.95).abs() < f64::EPSILON);
    assert!(!cluster.review_flag);
    assert!(
        !root.join("knowledge/concepts/gobby-daemon.md").exists(),
        "near-duplicate update must not create a sibling page"
    );

    // The would-be candidate resolved into an existing page: the audit
    // trail records a merge, and the target page is not quarantined.
    let log = fs::read_to_string(root.join("log.md")).expect("log written");
    assert!(log.contains("candidate_merged:"), "{log}");
    let target = fs::read_to_string(root.join("knowledge/concepts/long-running-service.md"))
        .expect("target page");
    assert!(
        !target.contains("candidate: true"),
        "merge target must not enter quarantine: {target}"
    );
}

#[test]
fn upkeep_review_band_creates_page_flagged_for_review() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(root, "src-a", "The [[Gobby Daemon]] never sleeps.\n");
    seed_source(root, "src-b", "Restart the [[Gobby Daemon]] nightly.\n");
    write_file(
        root,
        "knowledge/concepts/long-running-service.md",
        "---\ntitle: Long Running Service\n---\n\n# Long Running Service\n\nBody.\n",
    );

    let mut backend = FixedSemanticBackend {
        hits: vec![semantic_hit(
            "knowledge/concepts/long-running-service.md",
            0.85,
        )],
    };
    let probe = SemanticProbe {
        backend: &mut backend,
        search_scope: SearchScope::topic("upkeep-test"),
    };
    let report = run(
        research_scope(root),
        scope(),
        &Options::default(),
        Some(probe),
        None,
        TIMESTAMP,
    )
    .expect("upkeep run");

    let cluster = &report.clusters[0];
    assert_eq!(cluster.action, "created");
    assert!(cluster.review_flag, "review band flags the new page");
    assert!(cluster.near_duplicate.is_some());
    assert_eq!(
        cluster.page_path.as_deref(),
        Some(Path::new("knowledge/concepts/gobby-daemon.md"))
    );
}

#[test]
fn upkeep_respects_page_and_source_budgets() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    // Cluster "alpha": three sources; cluster "beta"/"gamma": one source each.
    seed_source(root, "src-a1", "On [[alpha]].\n");
    seed_source(root, "src-a2", "On [[alpha]].\n");
    seed_source(root, "src-a3", "On [[alpha]].\n");
    seed_source(root, "src-b", "On [[beta]] and [[beta]].\n");
    seed_source(root, "src-c", "On [[gamma]] and [[gamma]].\n");

    let report = run(
        research_scope(root),
        scope(),
        &Options {
            max_pages: 1,
            max_sources_per_page: 2,
            ..Options::default()
        },
        None,
        None,
        TIMESTAMP,
    )
    .expect("upkeep run");

    // "alpha" wins the budget slot (3 mentions beats 2).
    assert_eq!(report.clusters.len(), 1);
    let cluster = &report.clusters[0];
    assert_eq!(cluster.target, "alpha");
    assert_eq!(cluster.source_ids, vec!["src-a1", "src-a2"]);
    assert_eq!(cluster.sources_truncated, 1);
    assert_eq!(report.skipped_over_budget.len(), 2);

    // Budget-skipped and truncated sources stay pending for the next run.
    assert_eq!(compile_status_of(root, "src-a1"), CompileStatus::Compiled);
    assert_eq!(compile_status_of(root, "src-a2"), CompileStatus::Compiled);
    assert_eq!(compile_status_of(root, "src-a3"), CompileStatus::Pending);
    assert_eq!(compile_status_of(root, "src-b"), CompileStatus::Pending);
    assert_eq!(compile_status_of(root, "src-c"), CompileStatus::Pending);
    assert!(report.reconciled_no_synthesis.is_empty());
}

#[test]
fn time_budget_checkpoints_completed_clusters_and_defers_pending_work() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(root, "src-a1", "On [[alpha]].\n");
    seed_source(root, "src-a2", "On [[alpha]].\n");
    seed_source(root, "src-b1", "On [[beta]].\n");
    seed_source(root, "src-b2", "On [[beta]].\n");

    let started_at = Instant::now();
    let clock_calls = std::cell::Cell::new(0_usize);
    let report = run_with_clock(
        research_scope(root),
        scope(),
        &Options {
            // The 1320-second budget leaves 1209 seconds after the clock's
            // 111-second jump, one second below the 1210-second per-cluster
            // reservation, so the second cluster must be deferred.
            time_budget_seconds: Some(1320),
            ..Options::default()
        },
        None,
        None,
        TIMESTAMP,
        || {
            let call = clock_calls.get();
            clock_calls.set(call + 1);
            if call < 2 {
                started_at
            } else {
                started_at + Duration::from_secs(111)
            }
        },
    )
    .expect("time-budgeted upkeep run");

    assert_eq!(report.clusters.len(), 1);
    assert_eq!(report.clusters[0].target, "alpha");
    assert!(report.budget_exhausted);
    assert_eq!(report.deferred_clusters.len(), 1);
    assert_eq!(report.deferred_clusters[0].target, "beta");
    assert_eq!(compile_status_of(root, "src-a1"), CompileStatus::Compiled);
    assert_eq!(compile_status_of(root, "src-a2"), CompileStatus::Compiled);
    assert_eq!(compile_status_of(root, "src-b1"), CompileStatus::Pending);
    assert_eq!(compile_status_of(root, "src-b2"), CompileStatus::Pending);

    let report_path = root.join(REPORT_RELATIVE_PATH);
    let checkpoint: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&report_path).expect("read upkeep checkpoint"))
            .expect("parse upkeep checkpoint");
    assert_eq!(checkpoint["clusters"].as_array().map(Vec::len), Some(1));
    assert_eq!(checkpoint["budget_exhausted"], true);
    assert_eq!(checkpoint["deferred_clusters"][0]["target"], "beta");
    assert!(!report_path.with_extension("json.tmp").exists());
}

#[test]
fn upkeep_reconciles_unclustered_sources_as_reviewed_no_synthesis() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(root, "src-a", "On [[gcode]].\n");
    seed_source(root, "src-b", "On [[gcode]] too.\n");
    // Below min_mentions and no unresolved links at all: both reconcile.
    seed_source(root, "src-solo", "One [[Solo Target]] mention only.\n");
    seed_source(root, "src-plain", "No links here at all.\n");

    let report = run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("upkeep run");

    assert_eq!(
        report.reconciled_no_synthesis,
        vec!["src-plain".to_string(), "src-solo".to_string()]
    );
    assert_eq!(compile_status_of(root, "src-solo"), CompileStatus::Compiled);
    assert_eq!(
        compile_status_of(root, "src-plain"),
        CompileStatus::Compiled
    );
    assert_eq!(report.pending_after, 0);
}

#[test]
fn upkeep_records_per_page_failure_and_continues() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(root, "src-a", "On [[alpha]] and [[alpha]].\n");
    seed_source(root, "src-b", "On [[beta]] and [[beta]].\n");
    // Break cluster "alpha": its only raw source vanishes before the run.
    fs::remove_file(root.join("raw/src-a.md")).expect("remove raw source");

    let report = run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("upkeep run survives per-page failure");

    assert_eq!(report.failures, 1);
    assert_eq!(report.pages_created, 1);
    let failed = report
        .clusters
        .iter()
        .find(|cluster| cluster.action == "failed")
        .expect("failed cluster recorded");
    assert_eq!(failed.target, "alpha");
    assert!(
        failed
            .error
            .as_deref()
            .is_some_and(|error| !error.is_empty())
    );
    let created = report
        .clusters
        .iter()
        .find(|cluster| cluster.action == "created")
        .expect("other cluster still processed");
    assert_eq!(created.target, "beta");

    // The failed cluster's source stays pending; the drained one flips.
    assert_eq!(compile_status_of(root, "src-a"), CompileStatus::Pending);
    assert_eq!(compile_status_of(root, "src-b"), CompileStatus::Compiled);
}

#[test]
fn upkeep_heals_unresolved_concept_links_to_plain_text() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    // Two mentions cluster and synthesize (link kept resolved); the lone
    // mention stays unresolved and must be unwrapped to plain text with its
    // entity recorded for future runs.
    seed_source(root, "src-a", "Built on [[PostgreSQL]].\n");
    seed_source(root, "src-b", "Also uses [[PostgreSQL]].\n");
    seed_source(root, "src-solo", "## Connections\n\n- [[Ephemeral Idea]]\n");

    run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("upkeep run");

    let clustered =
        fs::read_to_string(root.join("knowledge/sources/src-a.md")).expect("read clustered digest");
    assert!(
        clustered.contains("[[PostgreSQL]]"),
        "synthesized concept link is kept resolved: {clustered}"
    );
    assert!(root.join("knowledge/concepts/postgresql.md").exists());

    let solo =
        fs::read_to_string(root.join("knowledge/sources/src-solo.md")).expect("read solo digest");
    assert!(
        !solo.contains("[[Ephemeral Idea]]"),
        "unresolved singleton link is unwrapped: {solo}"
    );
    assert!(
        solo.contains("- Ephemeral Idea"),
        "unwrapped mention stays as plain text: {solo}"
    );

    let registry =
        fs::read_to_string(root.join(HEALED_MENTIONS_PATH)).expect("read healed registry");
    assert!(
        registry.contains("Ephemeral Idea"),
        "healed entity recorded for later clustering: {registry}"
    );
}

#[test]
fn upkeep_healed_mentions_seed_a_later_cluster() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    // Run 1: a lone mention is healed to plain text and recorded.
    seed_source(root, "src-a", "First note on [[Emerging Topic]].\n");
    let first = run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("first upkeep run");
    assert_eq!(first.pages_created, 0, "a singleton does not synthesize");
    assert!(
        !fs::read_to_string(root.join("knowledge/sources/src-a.md"))
            .expect("read src-a")
            .contains("[[Emerging Topic]]"),
        "singleton healed to plain text on the first run"
    );

    // A second digest mentions the same concept on a later run.
    seed_source(root, "src-b", "Second note on [[Emerging Topic]].\n");
    let report = run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("second upkeep run");

    // The healed mention from run 1 plus the fresh mention now cluster, so
    // decoupling the work-queue from the red-links preserves discovery.
    assert_eq!(report.pages_created, 1);
    let cluster = report
        .clusters
        .iter()
        .find(|outcome| outcome.key == "emerging topic")
        .expect("emerging topic cluster");
    assert_eq!(cluster.mentions, 2);
    assert!(cluster.source_ids.contains(&"src-a".to_string()));
    assert!(cluster.source_ids.contains(&"src-b".to_string()));
}

#[test]
fn upkeep_heal_skips_concept_links_inside_code_spans() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    seed_source(
        root,
        "src-a",
        "Mentions `[[Not A Link]]` inline and [[Real Mention]] outside.\n",
    );
    run(
        research_scope(root),
        scope(),
        &Options::default(),
        None,
        None,
        TIMESTAMP,
    )
    .expect("upkeep run");
    let digest = fs::read_to_string(root.join("knowledge/sources/src-a.md")).expect("read digest");
    assert!(
        digest.contains("`[[Not A Link]]`"),
        "code-span content is left verbatim: {digest}"
    );
    assert!(
        !digest.contains("[[Real Mention]]"),
        "real unresolved link is unwrapped: {digest}"
    );
}

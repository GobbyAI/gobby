use std::path::{Path, PathBuf};

use gobby_core::config::{EmbeddingConfig, FalkorConfig, QdrantConfig};

use crate::ScopeIdentity;
use crate::frontmatter::parse_frontmatter;
use crate::markdown::parse_markdown;
use crate::search::semantic::SemanticSearchRequest;
use crate::sources::{SourceDraft, SourceManifest};
use crate::support::test_env::EnvGuard;

use super::semantic::{
    DISTINCT_PAIRS_RELATIVE_PATH, NearDuplicatePair, UnresolvedLinkCluster,
    expected_similarity_pair, near_duplicate_pairs, near_duplicate_query, unresolved_link_clusters,
};
use super::*;

#[test]
fn librarian_detects_and_proposes_without_rewriting_pages() {
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
        "---\ntitle: Stale\nstale: true\n---\n# Stale\nUnsupported operational claim.\nSee [[Missing]].\n",
    );
    let original_page =
        std::fs::read_to_string(root.join("knowledge/topics/stale.md")).expect("read page");
    let report = run(
        root,
        ScopeIdentity::project("project-1"),
        Options::offline(),
        None,
    )
    .expect("librarian runs");

    assert_eq!(
        report.check("stale_pages").items,
        vec![PathBuf::from("knowledge/topics/stale.md")]
    );
    assert_eq!(
        report.check("missing_citations").items,
        vec![PathBuf::from("knowledge/topics/stale.md")]
    );
    assert_eq!(
        report.check("broken_links").items,
        vec![PathBuf::from("knowledge/topics/stale.md")]
    );
    assert!(report.check("weak_provenance").items.is_empty());
    assert!(!report.check("patch_suggestions").available);
    assert!(
        report
            .suggested_tasks
            .iter()
            .any(|task| task.title.contains("Refresh stale wiki pages"))
    );
    assert!(
        report
            .suggested_patch_diffs
            .iter()
            .all(|diff| diff.applies_to_canonical_content)
    );
    assert_eq!(
        std::fs::read_to_string(root.join("knowledge/topics/stale.md")).expect("read page"),
        original_page
    );
    assert!(root.join("meta/librarian/proposals.json").exists());
    assert!(root.join("meta/librarian/audit-annotations.json").exists());
    assert!(root.join("meta/librarian/stale-pages.json").exists());
    assert!(
        report
            .suggested_tasks
            .iter()
            .any(|task| task.description.contains(&source.id))
    );
}

#[test]
fn generated_code_namespace_not_curated() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let code_page = "---\ntitle: Legacy Code\nstale: true\ngenerated_by: gcode-codewiki\n---\n# Legacy Code\nUnsupported operational claim.\n";
    write_page(root, "code/legacy.md", code_page);
    write_page(
        root,
        "knowledge/topics/control.md",
        "---\ntitle: Control\nstale: true\n---\n# Control\nUnsupported operational claim.\n",
    );

    let report = run(
        root,
        ScopeIdentity::project("project-1"),
        Options::offline(),
        None,
    )
    .expect("librarian runs");

    assert_eq!(
        std::fs::read_to_string(root.join("code/legacy.md")).expect("read code page"),
        code_page
    );
    let reported_paths = report
        .checks
        .iter()
        .flat_map(|check| check.items.iter())
        .chain(
            report
                .suggested_tasks
                .iter()
                .flat_map(|task| task.paths.iter()),
        )
        .chain(report.suggested_patch_diffs.iter().map(|diff| &diff.path))
        .cloned()
        .collect::<BTreeSet<_>>();
    assert!(!reported_paths.contains(Path::new("code/legacy.md")));
    assert!(reported_paths.contains(Path::new("knowledge/topics/control.md")));
}

#[test]
fn librarian_degrades_each_optional_check_independently() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    write_page(
        root,
        "knowledge/topics/page.md",
        "---\ntitle: Page\n---\n# Page\nSupported enough. [source](https://example.com)\n",
    );

    let report =
        run(root, ScopeIdentity::topic("ops"), Options::offline(), None).expect("librarian runs");

    assert!(report.check("stale_pages").available);
    assert!(report.check("missing_citations").available);
    assert!(report.check("broken_links").available);
    assert!(report.check("weak_provenance").available);
    assert!(!report.check("semantic_gaps").available);
    assert!(!report.check("patch_suggestions").available);
}

#[test]
fn librarian_probed_options_reflect_runtime_services() {
    let embedding = EmbeddingConfig {
        api_base: "http://localhost:1234/v1".to_string(),
        model: "embed-model".to_string(),
        api_key: None,
        query_prefix: None,
        timeout_seconds: 30,
    };
    let services = RuntimeServices {
        postgres_configured: true,
        falkor: Some(FalkorConfig {
            host: "localhost".to_string(),
            port: 6379,
            password: None,
        }),
        qdrant: Some(QdrantConfig {
            url: Some("http://localhost:6333".to_string()),
            api_key: None,
        }),
        embedding: Some(embedding.clone()),
        semantic_embedding: Some(crate::search::semantic::SemanticEmbedding::Direct(
            embedding,
        )),
    };

    assert_eq!(
        Options::probed(&services, true),
        Options {
            require_postgres_index: true,
            semantic_available: true,
            model_available: true,
        }
    );
    assert_eq!(
        Options::probed(&RuntimeServices::detached(), false),
        Options {
            require_postgres_index: true,
            ..Options::offline()
        }
    );
}

#[test]
fn librarian_semantic_gaps_report_near_duplicates_and_link_clusters() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    write_page(
        root,
        "knowledge/topics/async-rust.md",
        "# Async Rust\nSee [[Widget Factory]] twice: [[Widget Factory]] and once [[Solo Target]].\n",
    );
    write_page(
        root,
        "knowledge/topics/rust-async.md",
        "# Rust Async\nNearly the same content about async Rust.\n",
    );

    let mut backend = FixedSemanticBackend {
        hits: vec![
            semantic_hit("knowledge/topics/rust-async.md", 0.95),
            semantic_hit("code/files/dup.md", 0.99),
            semantic_hit("knowledge/topics/other.md", 0.50),
        ],
    };
    let report = run(
        root,
        ScopeIdentity::topic("ops"),
        Options {
            semantic_available: true,
            ..Options::offline()
        },
        Some(SemanticProbe {
            backend: &mut backend,
            search_scope: SearchScope::topic("ops"),
        }),
    )
    .expect("librarian runs");

    let check = report.check("semantic_gaps");
    assert!(check.available);
    assert!(check.note.is_none());
    assert_eq!(
        check.items,
        vec![
            PathBuf::from("Widget Factory"),
            PathBuf::from("knowledge/topics/async-rust.md"),
            PathBuf::from("knowledge/topics/rust-async.md"),
        ]
    );
    let merge_task = report
        .suggested_tasks
        .iter()
        .find(|task| task.title.contains("near-duplicate"))
        .expect("near-duplicate task");
    assert!(
        merge_task.description.contains("rust-async.md"),
        "{merge_task:?}"
    );
    assert!(merge_task.description.contains("0.95"), "{merge_task:?}");
    let create_task = report
        .suggested_tasks
        .iter()
        .find(|task| task.title.contains("repeatedly mentioned"))
        .expect("link cluster task");
    assert!(
        create_task
            .description
            .contains("Widget Factory (2 mentions)"),
        "{create_task:?}"
    );
    assert!(
        !create_task.description.contains("Solo Target"),
        "single mentions must not cluster: {create_task:?}"
    );
}

#[test]
fn librarian_semantic_gaps_fail_closed_without_backend_or_on_degradation() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    write_page(
        root,
        "knowledge/topics/page.md",
        "# Page\nSee [[Widget Factory]] and [[Widget Factory]].\n",
    );
    let options = Options {
        semantic_available: true,
        ..Options::offline()
    };

    let report =
        run(root, ScopeIdentity::topic("ops"), options.clone(), None).expect("librarian runs");
    let check = report.check("semantic_gaps");
    assert!(!check.available);
    assert!(check.items.is_empty());
    assert!(
        check
            .note
            .as_deref()
            .is_some_and(|note| note.contains("no semantic backend")),
        "{check:?}"
    );

    let mut degraded_backend = DegradedSemanticBackend;
    let report = run(
        root,
        ScopeIdentity::topic("ops"),
        options,
        Some(SemanticProbe {
            backend: &mut degraded_backend,
            search_scope: SearchScope::topic("ops"),
        }),
    )
    .expect("librarian runs");
    let check = report.check("semantic_gaps");
    assert!(!check.available);
    assert!(check.items.is_empty());
    assert!(
        check
            .note
            .as_deref()
            .is_some_and(|note| note.contains("degraded")),
        "{check:?}"
    );
}

#[test]
fn broken_link_classification_excludes_upkeep_convergent_mentions() {
    let digest_pages: BTreeSet<PathBuf> = [
        PathBuf::from("knowledge/sources/src-aaaa.md"),
        PathBuf::from("knowledge/sources/src-bbbb.md"),
    ]
    .into();
    let manifest_ids: BTreeSet<String> = ["src-aaaa".to_string(), "src-bbbb".to_string()].into();
    let issues = vec![
        // Case-variant digest mentions of one entity form a cluster.
        link_issue("knowledge/sources/src-aaaa.md", "Gobby"),
        link_issue("knowledge/sources/src-bbbb.md", "gobby"),
        // A lone digest mention stays convergence signal, not repair debt.
        link_issue("knowledge/sources/src-aaaa.md", "FalkorDB"),
        // Non-digest mentions of a digest-sustained entity self-heal too.
        link_issue("knowledge/concepts/overview.md", "Gobby"),
        // Registered but uncompiled digest target materializes on compile.
        link_issue(
            "knowledge/concepts/overview.md",
            "knowledge/sources/src-bbbb",
        ),
    ];

    let scan = classify_broken_links(&issues, &digest_pages, &manifest_ids);

    assert_eq!(scan.repair_pages, Vec::<PathBuf>::new());
    assert_eq!(scan.pending_clusters, 1);
    assert_eq!(scan.pending_singleton_mentions, 1);
    assert_eq!(scan.pending_compile_mentions, 1);
    let note = scan.pending_note().expect("pending note");
    assert!(note.contains("pending synthesis: 1 cluster(s), 1 singleton mention(s)"));
    assert!(note.contains("pending compile: 1 digest link(s)"));
}

#[test]
fn concept_worthiness_gates_broken_link_classification() {
    let digest_pages: BTreeSet<PathBuf> = [
        PathBuf::from("knowledge/sources/src-aaaa.md"),
        PathBuf::from("knowledge/sources/src-bbbb.md"),
    ]
    .into();
    let issues = vec![
        link_issue("knowledge/sources/src-aaaa.md", "awk"),
        link_issue("knowledge/sources/src-bbbb.md", "AWK"),
        link_issue("knowledge/concepts/overview.md", "awk"),
        link_issue("knowledge/sources/src-aaaa.md", "FalkorDB"),
        link_issue("knowledge/sources/src-bbbb.md", "falkordb"),
    ];

    let scan = classify_broken_links(&issues, &digest_pages, &BTreeSet::new());

    assert_eq!(scan.pending_clusters, 1);
    assert_eq!(scan.pending_singleton_mentions, 0);
    assert_eq!(
        scan.repair_pages,
        vec![
            PathBuf::from("knowledge/concepts/overview.md"),
            PathBuf::from("knowledge/sources/src-aaaa.md"),
            PathBuf::from("knowledge/sources/src-bbbb.md"),
        ]
    );
}

#[test]
fn broken_link_classification_keeps_dead_links_as_repair_debt() {
    let digest_pages: BTreeSet<PathBuf> = [PathBuf::from("knowledge/sources/src-aaaa.md")].into();
    let manifest_ids: BTreeSet<String> = ["src-aaaa".to_string()].into();
    let issues = vec![
        // Digest target with no manifest record behind it (purged row).
        link_issue(
            "knowledge/topics/overview.md",
            "knowledge/sources/src-gone.md",
        ),
        // Path-shaped target can never become an entity page, even when a
        // digest mentions it.
        link_issue("knowledge/sources/src-aaaa.md", "code/files/foo.md"),
        // Entity mention no digest sustains has no convergence path.
        link_issue("knowledge/concepts/overview.md", "Orphan"),
    ];

    let scan = classify_broken_links(&issues, &digest_pages, &manifest_ids);

    assert_eq!(
        scan.repair_pages,
        vec![
            PathBuf::from("knowledge/concepts/overview.md"),
            PathBuf::from("knowledge/sources/src-aaaa.md"),
            PathBuf::from("knowledge/topics/overview.md"),
        ]
    );
    assert_eq!(scan.pending_clusters, 0);
    assert_eq!(scan.pending_singleton_mentions, 0);
    assert_eq!(scan.pending_compile_mentions, 0);
    assert_eq!(scan.pending_note(), None);
}

#[test]
fn librarian_excludes_digest_entity_mentions_from_repair_proposals() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let first = SourceManifest::register(
        root,
        SourceDraft::url("https://example.com/one", "2026-07-01T00:00:00Z", "one"),
    )
    .expect("register first source");
    let second = SourceManifest::register(
        root,
        SourceDraft::url("https://example.com/two", "2026-07-01T00:00:00Z", "two"),
    )
    .expect("register second source");
    write_page(
        root,
        &format!("knowledge/sources/{}.md", first.id),
        "# One\nMentions [[PendingEntity]].\n",
    );
    write_page(
        root,
        &format!("knowledge/sources/{}.md", second.id),
        "# Two\nMentions [[pendingentity]].\n",
    );
    write_page(
        root,
        "knowledge/topics/dead.md",
        "# Dead\nSee [[knowledge/sources/src-0000000000000000-gone]].\n",
    );

    let report = run(
        root,
        ScopeIdentity::project("project-1"),
        Options::offline(),
        None,
    )
    .expect("librarian runs");

    let check = report.check("broken_links");
    assert_eq!(check.items, vec![PathBuf::from("knowledge/topics/dead.md")]);
    let note = check.note.as_deref().expect("pending note");
    assert!(note.contains("pending synthesis: 1 cluster(s)"));
    let repair = report
        .suggested_tasks
        .iter()
        .find(|task| task.title == "Repair broken wiki links")
        .expect("repair task proposed for the dead link");
    assert_eq!(
        repair.paths,
        vec![PathBuf::from("knowledge/topics/dead.md")]
    );
}

#[test]
fn unresolved_link_clusters_fold_case_and_require_multiple_mentions() {
    let issues = vec![
        link_issue("a.md", "Widget Factory"),
        link_issue("b.md", "widget factory"),
        link_issue("a.md", "Solo"),
    ];

    assert_eq!(
        unresolved_link_clusters(&issues),
        vec![UnresolvedLinkCluster {
            target: "Widget Factory".to_string(),
            mentions: 2,
        }]
    );
}

#[test]
fn concept_worthiness_gates_semantic_link_clusters() {
    let issues = vec![
        link_issue("a.md", "awk"),
        link_issue("b.md", "AWK"),
        link_issue("a.md", "FalkorDB"),
        link_issue("b.md", "falkordb"),
    ];

    assert_eq!(
        unresolved_link_clusters(&issues),
        vec![UnresolvedLinkCluster {
            target: "FalkorDB".to_string(),
            mentions: 2,
        }]
    );
}

#[test]
fn unresolved_link_clusters_skip_absolute_filesystem_targets() {
    // #17649: scratchpad links in session digests cluster like any other
    // repeated unresolved target, but an absolute filesystem path can
    // never be a vault page — no page-creation proposal.
    let issues = vec![
        link_issue("a.md", "/private/tmp/claude-501/scratchpad/note-orchid.md"),
        link_issue("b.md", "/private/tmp/claude-501/scratchpad/note-orchid.md"),
    ];

    assert_eq!(unresolved_link_clusters(&issues), Vec::new());
}

#[test]
fn near_duplicates_skip_sources_cited_by_the_synthesis() {
    let concept = knowledge_page(
        "knowledge/concepts/gcode.md",
        "---\ntitle: gcode\nsource_kind: concept\n---\n# gcode\n\nSources: [[knowledge/sources/src-1-session-aaa|Session: aaa]]\n\ngcode is the code index CLI.\n",
    );
    let digest = knowledge_page(
        "knowledge/sources/src-1-session-aaa.md",
        "---\nsource_kind: session\n---\n# Session: aaa\n\ngcode is the code index CLI.\n",
    );
    let mut backend = FixedSemanticBackend {
        hits: vec![
            semantic_hit("knowledge/concepts/gcode.md", 0.95),
            semantic_hit("knowledge/sources/src-1-session-aaa.md", 0.95),
        ],
    };

    let pairs = near_duplicate_pairs(
        &[concept, digest],
        SemanticProbe {
            backend: &mut backend,
            search_scope: SearchScope::topic("ops"),
        },
        &BTreeSet::new(),
    )
    .expect("scan succeeds");

    assert_eq!(pairs, Vec::new());
}

#[test]
fn near_duplicates_skip_session_digest_pairs() {
    let first = knowledge_page(
        "knowledge/sources/src-1-session-aaa.md",
        "---\nsource_kind: session\n---\n# Session: aaa\n\nWorked the wiki upkeep pipeline.\n",
    );
    let second = knowledge_page(
        "knowledge/sources/src-2-session-bbb.md",
        "---\nsource_kind: session\n---\n# Session: bbb\n\nContinued the wiki upkeep pipeline.\n",
    );
    let mut backend = FixedSemanticBackend {
        hits: vec![
            semantic_hit("knowledge/sources/src-1-session-aaa.md", 0.92),
            semantic_hit("knowledge/sources/src-2-session-bbb.md", 0.92),
        ],
    };

    let pairs = near_duplicate_pairs(
        &[first, second],
        SemanticProbe {
            backend: &mut backend,
            search_scope: SearchScope::topic("ops"),
        },
        &BTreeSet::new(),
    )
    .expect("scan succeeds");

    assert_eq!(pairs, Vec::new());
}

#[test]
fn near_duplicates_skip_stale_semantic_hits() {
    let current = knowledge_page(
        "knowledge/sources/src-ac09859b71d30030-session-019f917d-db1d-7a01-97f1-13e11eacdd31.md",
        "---\nsource_kind: session\n---\n# Session: current\n\nWorked the wiki upkeep pipeline.\n",
    );
    let mut backend = FixedSemanticBackend {
        hits: vec![semantic_hit(
            "knowledge/sources/src-3e33ee89f4af7707-session-019f917d-db1d-7a01-97f1-13e11eacdd31.md",
            0.95,
        )],
    };

    let pairs = near_duplicate_pairs(
        &[current],
        SemanticProbe {
            backend: &mut backend,
            search_scope: SearchScope::topic("ops"),
        },
        &BTreeSet::new(),
    )
    .expect("scan succeeds");

    assert_eq!(pairs, Vec::new());
}

#[test]
fn near_duplicates_skip_redirect_pairs() {
    let redirect = knowledge_page(
        "knowledge/topics/legacy-topic.md",
        "---\nsource_kind: topic\nredirect: knowledge/topics/canonical-topic\n---\n# Legacy topic\n\n## Backlinks\n\n- [[knowledge/topics/canonical-topic|Canonical topic]]\n",
    );
    let source = knowledge_page(
        "knowledge/sources/source.md",
        "---\nsource_kind: source_note\n---\n# Source\n\nCanonical topic research.\n",
    );
    let mut backend = FixedSemanticBackend {
        hits: vec![
            semantic_hit("knowledge/topics/legacy-topic.md", 0.92),
            semantic_hit("knowledge/sources/source.md", 0.92),
        ],
    };

    let pairs = near_duplicate_pairs(
        &[redirect, source],
        SemanticProbe {
            backend: &mut backend,
            search_scope: SearchScope::topic("ops"),
        },
        &BTreeSet::new(),
    )
    .expect("scan succeeds");

    assert_eq!(pairs, Vec::new());
}

#[test]
fn near_duplicates_keep_concept_pairs() {
    let left = knowledge_page(
        "knowledge/concepts/gobby-core.md",
        "---\nsource_kind: concept\n---\n# gobby-core\n\nShared Rust library crate.\n",
    );
    let right = knowledge_page(
        "knowledge/concepts/gcore.md",
        "---\nsource_kind: concept\n---\n# gcore\n\nShared Rust library crate.\n",
    );
    let mut backend = FixedSemanticBackend {
        hits: vec![
            semantic_hit("knowledge/concepts/gobby-core.md", 0.93),
            semantic_hit("knowledge/concepts/gcore.md", 0.93),
        ],
    };

    let pairs = near_duplicate_pairs(
        &[left, right],
        SemanticProbe {
            backend: &mut backend,
            search_scope: SearchScope::topic("ops"),
        },
        &BTreeSet::new(),
    )
    .expect("scan succeeds");

    assert_eq!(
        pairs,
        vec![NearDuplicatePair {
            left: PathBuf::from("knowledge/concepts/gcore.md"),
            right: PathBuf::from("knowledge/concepts/gobby-core.md"),
            score: 0.93,
        }]
    );
}

#[test]
fn near_duplicates_keep_source_pairs_the_synthesis_does_not_cite() {
    let concept = knowledge_page(
        "knowledge/concepts/gcode.md",
        "---\nsource_kind: concept\n---\n# gcode\n\nSources: [[knowledge/sources/src-9-session-zzz|Session: zzz]]\n\ngcode is the code index CLI.\n",
    );
    let digest = knowledge_page(
        "knowledge/sources/src-1-session-aaa.md",
        "---\nsource_kind: session\n---\n# Session: aaa\n\ngcode is the code index CLI.\n",
    );
    let mut backend = FixedSemanticBackend {
        hits: vec![
            semantic_hit("knowledge/concepts/gcode.md", 0.94),
            semantic_hit("knowledge/sources/src-1-session-aaa.md", 0.94),
        ],
    };

    let pairs = near_duplicate_pairs(
        &[concept, digest],
        SemanticProbe {
            backend: &mut backend,
            search_scope: SearchScope::topic("ops"),
        },
        &BTreeSet::new(),
    )
    .expect("scan succeeds");

    assert_eq!(
        pairs,
        vec![NearDuplicatePair {
            left: PathBuf::from("knowledge/concepts/gcode.md"),
            right: PathBuf::from("knowledge/sources/src-1-session-aaa.md"),
            score: 0.94,
        }]
    );
}

#[test]
fn near_duplicates_skip_generated_folder_contexts() {
    // Per-folder `_context.md` navigation files share one template, so
    // sibling contexts always score high; they are generated surfaces,
    // never merge candidates (#17782).
    let root_context = knowledge_page(
        "knowledge/_context.md",
        "---\ntitle: knowledge — folder context\n---\n\n# knowledge\n\n## Pages (2)\n",
    );
    let concepts_context = knowledge_page(
        "knowledge/concepts/_context.md",
        "---\ntitle: concepts — folder context\n---\n\n# concepts\n\n## Pages (5)\n",
    );
    let mut backend = FixedSemanticBackend {
        hits: vec![
            semantic_hit("knowledge/_context.md", 0.93),
            semantic_hit("knowledge/concepts/_context.md", 0.93),
        ],
    };

    let pairs = near_duplicate_pairs(
        &[root_context, concepts_context],
        SemanticProbe {
            backend: &mut backend,
            search_scope: SearchScope::topic("ops"),
        },
        &BTreeSet::new(),
    )
    .expect("scan succeeds");

    assert_eq!(pairs, Vec::new());
}

#[test]
fn near_duplicates_skip_structural_backlog_as_query_and_hit() {
    let topic = knowledge_page(
        "knowledge/topics/hacker-news-rowboat-and-lowfat-2026-07-17.md",
        "# Hacker News: Rowboat and Lowfat\n\nA focused research topic.\n",
    );
    let backlog = knowledge_page(
        "knowledge/topics/wiki-research-backlog.md",
        "# Wiki Research Backlog\n\nA structural research triage surface.\n",
    );
    let expected_query = near_duplicate_query(&topic);
    let mut backend = RecordingSemanticBackend {
        hits: vec![
            semantic_hit(
                "knowledge/topics/hacker-news-rowboat-and-lowfat-2026-07-17.md",
                0.91,
            ),
            semantic_hit("knowledge/topics/wiki-research-backlog.md", 0.91),
        ],
        queries: Vec::new(),
    };

    let pairs = near_duplicate_pairs(
        &[topic, backlog],
        SemanticProbe {
            backend: &mut backend,
            search_scope: SearchScope::topic("ops"),
        },
        &BTreeSet::new(),
    )
    .expect("scan succeeds");

    assert_eq!(pairs, Vec::new());
    assert_eq!(backend.queries, vec![expected_query]);
}

struct FixedSemanticBackend {
    hits: Vec<crate::search::WikiSearchResult>,
}

impl SemanticSearchBackend for FixedSemanticBackend {
    fn search_semantic(
        &mut self,
        _request: SemanticSearchRequest,
    ) -> Result<crate::search::semantic::SemanticSearchOutcome, crate::search::SearchError> {
        Ok(crate::search::semantic::SemanticSearchOutcome {
            hits: self.hits.clone(),
            degradation: None,
        })
    }
}

struct RecordingSemanticBackend {
    hits: Vec<crate::search::WikiSearchResult>,
    queries: Vec<String>,
}

impl SemanticSearchBackend for RecordingSemanticBackend {
    fn search_semantic(
        &mut self,
        request: SemanticSearchRequest,
    ) -> Result<crate::search::semantic::SemanticSearchOutcome, crate::search::SearchError> {
        self.queries.push(request.query);
        Ok(crate::search::semantic::SemanticSearchOutcome {
            hits: self.hits.clone(),
            degradation: None,
        })
    }
}

struct DegradedSemanticBackend;

impl SemanticSearchBackend for DegradedSemanticBackend {
    fn search_semantic(
        &mut self,
        _request: SemanticSearchRequest,
    ) -> Result<crate::search::semantic::SemanticSearchOutcome, crate::search::SearchError> {
        Ok(crate::search::semantic::SemanticSearchOutcome {
            hits: Vec::new(),
            degradation: Some(gobby_core::degradation::DegradationKind::PartialData {
                component: "semantic".to_string(),
                message: "qdrant unreachable".to_string(),
            }),
        })
    }
}

fn semantic_hit(path: &str, score: f64) -> crate::search::WikiSearchResult {
    crate::search::WikiSearchResult {
        id: path.to_string(),
        title: None,
        scope: SearchScope::topic("ops"),
        path: PathBuf::from(path),
        source_path: PathBuf::from(path),
        hit_kind: crate::search::SearchHitKind::Document,
        snippet: String::new(),
        score,
        sources: Vec::new(),
        explanations: Vec::new(),
        chunk: None,
        provenance: crate::search::SearchProvenance {
            document_path: PathBuf::from(path),
            source_path: PathBuf::from(path),
            source_kind: "document".to_string(),
            content_hash: None,
        },
    }
}

fn link_issue(path: &str, target: &str) -> lint::LinkIssue {
    lint::LinkIssue {
        path: PathBuf::from(path),
        line: 1,
        target: target.to_string(),
        kind: "wikilink".to_string(),
    }
}

#[test]
#[serial_test::serial]
fn librarian_requires_configured_postgres_index() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    write_page(
        root,
        "knowledge/topics/page.md",
        "# Page\n\nSupported enough.\n",
    );
    let _database_url = EnvGuard::set("GWIKI_TEST_DATABASE_URL", "postgresql://127.0.0.1:1/gwiki");

    let error = run(root, ScopeIdentity::topic("ops"), Options::default(), None)
        .expect_err("PostgreSQL is required");

    assert!(
        error
            .to_string()
            .contains("failed to connect to PostgreSQL for gwiki librarian"),
        "{error}"
    );
}

fn write_page(root: &Path, relative: &str, markdown: &str) {
    let path = root.join(relative);
    std::fs::create_dir_all(path.parent().expect("page parent")).expect("create parent");
    std::fs::write(path, markdown).expect("write page");
}

fn promotion_report(checks: Vec<CheckReport>) -> ProposalsReport {
    ProposalsReport {
        scope: ScopeIdentity::project("proj"),
        checks,
        suggested_tasks: Vec::new(),
        suggested_patch_diffs: Vec::new(),
        artifacts: artifacts(),
        dependency_classification: DependencyClassification {
            hard: vec!["vault"],
            optional: vec![],
            multimodal: "none",
        },
    }
}

fn hygiene_checks(implicated: &[(&'static str, &str)]) -> Vec<CheckReport> {
    [
        "stale_pages",
        "missing_citations",
        "broken_links",
        "weak_provenance",
        "semantic_gaps",
        "patch_suggestions",
    ]
    .into_iter()
    .map(|name| {
        let items = implicated
            .iter()
            .filter(|(check, _)| *check == name)
            .map(|(_, path)| PathBuf::from(*path))
            .collect();
        CheckReport {
            name,
            available: true,
            note: None,
            items,
        }
    })
    .collect()
}

#[test]
fn clean_pass_promotes_draft_pages_to_reviewed() {
    let temp = tempfile::tempdir().expect("tempdir");
    let clean = "---\ntitle: Clean\nlifecycle: draft\n---\n\nBody.\n";
    let flagged = "---\ntitle: Flagged\nlifecycle: draft\n---\n\nBody.\n";
    let legacy = "---\ntitle: Legacy\n---\n\nBody.\n";
    write_page(temp.path(), "knowledge/concepts/clean.md", clean);
    write_page(temp.path(), "knowledge/concepts/flagged.md", flagged);
    write_page(temp.path(), "knowledge/concepts/legacy.md", legacy);
    let pages = vec![
        knowledge_page("knowledge/concepts/clean.md", clean),
        knowledge_page("knowledge/concepts/flagged.md", flagged),
        knowledge_page("knowledge/concepts/legacy.md", legacy),
    ];
    let report = promotion_report(hygiene_checks(&[(
        "weak_provenance",
        "knowledge/concepts/flagged.md",
    )]));

    promote_reviewed_lifecycle(temp.path(), &report, &pages).expect("promotion pass");

    let lifecycle_of = |relative: &str| {
        let markdown = std::fs::read_to_string(temp.path().join(relative)).expect("read page");
        parse_frontmatter(&markdown)
            .expect("parse page")
            .metadata
            .lifecycle
    };
    use crate::frontmatter::WikiLifecycle;
    assert_eq!(
        lifecycle_of("knowledge/concepts/clean.md"),
        Some(WikiLifecycle::Reviewed)
    );
    assert_eq!(
        lifecycle_of("knowledge/concepts/flagged.md"),
        Some(WikiLifecycle::Draft)
    );
    assert_eq!(lifecycle_of("knowledge/concepts/legacy.md"), None);

    let log = std::fs::read_to_string(temp.path().join("log.md")).expect("read log");
    assert_eq!(log.matches("lifecycle_transition:").count(), 1, "{log}");
    assert!(log.contains("draft -> reviewed"), "{log}");
}

#[test]
fn degraded_hygiene_pass_never_promotes() {
    let temp = tempfile::tempdir().expect("tempdir");
    let clean = "---\ntitle: Clean\nlifecycle: draft\n---\n\nBody.\n";
    write_page(temp.path(), "knowledge/concepts/clean.md", clean);
    let pages = vec![knowledge_page("knowledge/concepts/clean.md", clean)];

    // semantic_gaps unavailable => not a clean pass; no promotion.
    let mut checks = hygiene_checks(&[]);
    checks
        .iter_mut()
        .find(|check| check.name == "semantic_gaps")
        .expect("semantic check")
        .available = false;
    promote_reviewed_lifecycle(temp.path(), &promotion_report(checks), &pages)
        .expect("degraded pass");
    use crate::frontmatter::WikiLifecycle;
    let markdown = std::fs::read_to_string(temp.path().join("knowledge/concepts/clean.md"))
        .expect("read page");
    assert_eq!(
        parse_frontmatter(&markdown)
            .expect("parse")
            .metadata
            .lifecycle,
        Some(WikiLifecycle::Draft)
    );

    // patch_suggestions availability gates patch output, not page hygiene:
    // its absence alone still promotes.
    let mut checks = hygiene_checks(&[]);
    checks
        .iter_mut()
        .find(|check| check.name == "patch_suggestions")
        .expect("patch check")
        .available = false;
    promote_reviewed_lifecycle(temp.path(), &promotion_report(checks), &pages)
        .expect("promotion pass");
    let markdown = std::fs::read_to_string(temp.path().join("knowledge/concepts/clean.md"))
        .expect("read page");
    assert_eq!(
        parse_frontmatter(&markdown)
            .expect("parse")
            .metadata
            .lifecycle,
        Some(WikiLifecycle::Reviewed)
    );
}

fn knowledge_page(relative: &str, markdown: &str) -> lint::WikiPage {
    let relative_path = PathBuf::from(relative);
    lint::WikiPage {
        path: relative_path.clone(),
        relative_path: relative_path.clone(),
        parsed: parse_markdown(relative_path, markdown, Vec::<String>::new())
            .expect("parse test page"),
        markdown: markdown.to_string(),
        has_frontmatter: markdown.starts_with("---"),
    }
}

#[test]
fn distinct_pairs_verdict_suppresses_near_duplicate_pair() {
    // A reviewed disambiguation verdict (#17782) recorded in
    // meta/librarian/distinct-pairs.json suppresses the pair in either
    // ordering and either path spelling; unrelated pairs keep flagging.
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    std::fs::create_dir_all(root.join("meta/librarian")).expect("create meta dir");
    std::fs::write(
        root.join(DISTINCT_PAIRS_RELATIVE_PATH),
        r#"{"pairs": [{"left": "knowledge/concepts/transport", "right": "knowledge/concepts/action.md"}]}"#,
    )
    .expect("write verdicts");
    let distinct_pairs = load_distinct_pairs(root);

    let pages_by_path: BTreeMap<&Path, &lint::WikiPage> = BTreeMap::new();
    assert!(expected_similarity_pair(
        Path::new("knowledge/concepts/action.md"),
        Path::new("knowledge/concepts/transport.md"),
        &pages_by_path,
        &distinct_pairs,
    ));
    assert!(expected_similarity_pair(
        Path::new("knowledge/concepts/transport.md"),
        Path::new("knowledge/concepts/action.md"),
        &pages_by_path,
        &distinct_pairs,
    ));
    assert!(!expected_similarity_pair(
        Path::new("knowledge/concepts/action.md"),
        Path::new("knowledge/concepts/unrelated.md"),
        &pages_by_path,
        &distinct_pairs,
    ));

    // Missing file loads as an empty set.
    let empty = tempfile::tempdir().expect("tempdir");
    assert!(load_distinct_pairs(empty.path()).is_empty());
}

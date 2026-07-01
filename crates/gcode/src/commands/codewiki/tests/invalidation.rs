//! Leaf H (#893): per-page-type invalidation keys, cross-file neighbor
//! invalidation, and the `--since` incremental driver.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use super::support::*;
use super::*;

/// Writes `content` to `rel` under `root`, creating parent dirs. Source files
/// must exist on disk because persistence hashes every page's cited provenance.
fn write_source(root: &Path, rel: &str, content: &str) {
    let path = root.join(rel);
    std::fs::create_dir_all(path.parent().expect("parent dir")).expect("create dirs");
    std::fs::write(path, content).expect("write source");
}

/// A minimal two-crate workspace model. `extra_edge` adds a dependency edge so a
/// caller can simulate a Cargo.toml change that shifts the SystemModel digest.
fn workspace_model(extra_edge: bool) -> SystemModel {
    let mut features_by_crate = BTreeMap::new();
    features_by_crate.insert("gobby-code".to_string(), vec!["postgres".to_string()]);
    let mut edges = vec![Edge {
        from: "gobby-code".to_string(),
        to: "gobby-core".to_string(),
    }];
    if extra_edge {
        edges.push(Edge {
            from: "gobby-hooks".to_string(),
            to: "gobby-core".to_string(),
        });
    }
    SystemModel {
        crates: vec![
            Crate {
                name: "gobby-code".to_string(),
                path: "crates/gcode".to_string(),
                is_binary: true,
                is_lib: false,
            },
            Crate {
                name: "gobby-core".to_string(),
                path: "crates/gcore".to_string(),
                is_binary: false,
                is_lib: true,
            },
        ],
        edges,
        services: vec![ServiceBoundary {
            name: "PostgreSQL hub".to_string(),
            kind: ServiceKind::Postgres,
            pulled_in_by: vec!["gobby-code (feature: postgres)".to_string()],
        }],
        runtime_modes: vec![RuntimeMode::Standalone, RuntimeMode::DaemonAttached],
        features_by_crate,
        notes: Vec::new(),
    }
}

/// Runs one full unscoped generation with reuse + persistence and returns the
/// set of pages the run actually rewrote (the incremental "changed set").
fn run_generate(
    input: &CodewikiInput,
    model: Option<&SystemModel>,
    project_root: &Path,
    out_dir: &Path,
    since: Option<BTreeSet<String>>,
) -> Vec<String> {
    run_generate_with_audit(input, model, None, project_root, out_dir, since)
}

fn run_generate_with_audit(
    input: &CodewikiInput,
    model: Option<&SystemModel>,
    audit: Option<&AuditContext>,
    project_root: &Path,
    out_dir: &Path,
    since: Option<BTreeSet<String>>,
) -> Vec<String> {
    let mut reuse_plan =
        ReusePlan::load_with_since(project_root, out_dir, "symbols", since.clone())
            .expect("reuse plan loads");
    let mut reuse = Some(&mut reuse_plan);
    let mut generate = None::<&mut TextGenerator<'_>>;
    let mut progress = CodewikiProgress::silent();
    let doc_scope = DocPruneScope::unscoped();
    let mut sink =
        DocSink::open_with_prune_scope(project_root, out_dir, "symbols", doc_scope.clone())
            .expect("sink opens")
            .with_since(since);
    generate_hierarchical_docs_core(
        input,
        None,
        model,
        None,
        audit,
        &mut generate,
        &mut None,
        &mut None,
        AiDepth::Symbols,
        VerifyScope::All,
        CodewikiAiOutcome::default(),
        &mut reuse,
        &mut progress,
        &doc_scope,
        &mut |doc| sink.persist(&doc).map(|_| ()),
    )
    .expect("generate docs");
    sink.finish(None).expect("finish")
}

/// Two files in distinct subsystems so the architecture page is built.
fn two_crate_input() -> CodewikiInput {
    CodewikiInput {
        leading_chunks: BTreeMap::new(),
        files: vec![
            "crates/gcode/src/lib.rs".to_string(),
            "crates/gcore/src/lib.rs".to_string(),
        ],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol(
                "crates/gcode/src/lib.rs",
                "Code",
                "class",
                1,
                "pub struct Code;",
            ),
            test_symbol(
                "crates/gcore/src/lib.rs",
                "Core",
                "class",
                1,
                "pub struct Core;",
            ),
        ],
    }
}

fn seed_two_crate_sources(root: &Path) {
    write_source(root, "crates/gcode/src/lib.rs", "pub struct Code;\n");
    write_source(root, "crates/gcore/src/lib.rs", "pub struct Core;\n");
}

#[test]
fn function_body_edit_keeps_keyed_aggregate_pages() {
    let project = tempfile::tempdir().expect("project");
    let out_dir = project.path().join("codewiki");
    seed_two_crate_sources(project.path());
    let input = two_crate_input();
    let model = workspace_model(false);

    let first = run_generate(&input, Some(&model), project.path(), &out_dir, None);
    assert!(first.iter().any(|p| p == "code/_architecture.md"));
    assert!(first.iter().any(|p| p == "code/infrastructure.md"));

    // Edit a function body: the file's source changes, the SystemModel — crates,
    // edges, services, features — does not.
    write_source(
        project.path(),
        "crates/gcode/src/lib.rs",
        "pub struct Code {\n    seq: u64,\n}\n",
    );

    let second = run_generate(&input, Some(&model), project.path(), &out_dir, None);
    assert!(
        second
            .iter()
            .any(|p| p == "code/files/crates/gcode/src/lib.rs.md"),
        "edited file page should rewrite: {second:?}"
    );
    assert!(
        !second.iter().any(|p| p == "code/_architecture.md"),
        "architecture is keyed on the SystemModel digest; a body edit must not rebuild it: {second:?}"
    );
    assert!(
        !second.iter().any(|p| p == "code/infrastructure.md"),
        "infrastructure is keyed on the SystemModel digest; a body edit must not rebuild it: {second:?}"
    );
}

#[test]
fn system_model_change_rewrites_architecture_not_unrelated_files() {
    let project = tempfile::tempdir().expect("project");
    let out_dir = project.path().join("codewiki");
    seed_two_crate_sources(project.path());
    let input = two_crate_input();

    let first = run_generate(
        &input,
        Some(&workspace_model(false)),
        project.path(),
        &out_dir,
        None,
    );
    assert!(first.iter().any(|p| p == "code/_architecture.md"));

    // A Cargo.toml dependency change shifts the SystemModel digest while no
    // source file on disk changed.
    let second = run_generate(
        &input,
        Some(&workspace_model(true)),
        project.path(),
        &out_dir,
        None,
    );
    assert!(
        second.iter().any(|p| p == "code/_architecture.md"),
        "a dependency-edge change must rebuild architecture: {second:?}"
    );
    assert!(
        second.iter().any(|p| p == "code/infrastructure.md"),
        "a dependency-edge change must rebuild infrastructure: {second:?}"
    );
    assert!(
        !second.iter().any(|p| p.starts_with("code/files/")),
        "no source file changed, so file pages must not rebuild: {second:?}"
    );
}

#[test]
fn leading_chunk_change_rewrites_architecture_with_same_system_model() {
    let project = tempfile::tempdir().expect("project");
    let out_dir = project.path().join("codewiki");
    seed_two_crate_sources(project.path());
    let mut input = two_crate_input();
    input.leading_chunks.insert(
        "crates/gcode/src/lib.rs".to_string(),
        LeadingChunk {
            content: "first architecture excerpt".to_string(),
            line_start: 1,
            line_end: 1,
        },
    );
    let model = workspace_model(false);

    let first = run_generate(&input, Some(&model), project.path(), &out_dir, None);
    assert!(first.iter().any(|p| p == "code/_architecture.md"));

    input.leading_chunks.insert(
        "crates/gcode/src/lib.rs".to_string(),
        LeadingChunk {
            content: "second architecture excerpt".to_string(),
            line_start: 1,
            line_end: 1,
        },
    );
    let second = run_generate(&input, Some(&model), project.path(), &out_dir, None);
    assert!(
        second.iter().any(|p| p == "code/_architecture.md"),
        "architecture prompt input changed, so the page must rebuild: {second:?}"
    );
    assert!(
        !second.iter().any(|p| p == "code/infrastructure.md"),
        "infrastructure is keyed only on the SystemModel digest and must not rebuild: {second:?}"
    );
}

#[test]
fn audit_link_change_rewrites_repo_overview_with_same_sources() {
    let project = tempfile::tempdir().expect("project");
    let out_dir = project.path().join("codewiki");
    seed_two_crate_sources(project.path());
    let input = two_crate_input();
    let model = workspace_model(false);

    let first = run_generate(&input, Some(&model), project.path(), &out_dir, None);
    assert!(first.iter().any(|p| p == "code/repo.md"));

    let audit = AuditContext {
        deprecations: BTreeMap::new(),
        tests: BTreeSet::new(),
    };
    let second = run_generate_with_audit(
        &input,
        Some(&model),
        Some(&audit),
        project.path(),
        &out_dir,
        None,
    );
    assert!(
        second.iter().any(|p| p == "code/repo.md"),
        "repo audit-link set changed, so the overview must rebuild: {second:?}"
    );
}

#[test]
fn caller_edit_invalidates_callee_page() {
    let project = tempfile::tempdir().expect("project");
    let out_dir = project.path().join("codewiki");
    write_source(project.path(), "alpha/a.rs", "pub fn a_fn() {}\n");
    write_source(project.path(), "beta/b.rs", "pub fn b_fn() {}\n");

    // alpha::a_fn calls beta::b_fn, so beta/b.rs records alpha/a.rs as a
    // cross-file neighbor (#885).
    let input = CodewikiInput {
        leading_chunks: BTreeMap::new(),
        files: vec!["alpha/a.rs".to_string(), "beta/b.rs".to_string()],
        graph_edges: vec![CodewikiGraphEdge::call(
            test_component_id("alpha/a.rs", "a_fn", "function"),
            test_component_id("beta/b.rs", "b_fn", "function"),
        )],
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol("alpha/a.rs", "a_fn", "function", 1, "pub fn a_fn()"),
            test_symbol("beta/b.rs", "b_fn", "function", 1, "pub fn b_fn()"),
        ],
    };

    let first = run_generate(&input, None, project.path(), &out_dir, None);
    assert!(first.iter().any(|p| p == "code/files/beta/b.rs.md"));

    // Edit the caller only; beta/b.rs's own source is untouched.
    write_source(
        project.path(),
        "alpha/a.rs",
        "pub fn a_fn() {\n    let _ = 1;\n}\n",
    );

    let second = run_generate(&input, None, project.path(), &out_dir, None);
    assert!(
        second.iter().any(|p| p == "code/files/beta/b.rs.md"),
        "callee page must rebuild when its caller neighbor changes: {second:?}"
    );
}

#[test]
fn derived_page_rewrites_only_when_key_changes() {
    let project = tempfile::tempdir().expect("project");
    let out_dir = project.path().join("codewiki");

    let write = |docs: Vec<BuiltDoc>| {
        write_incremental_doc_set_with_snapshot(
            project.path(),
            &out_dir,
            &docs,
            None,
            "symbols",
            DocPruneScope::unscoped(),
        )
        .expect("write derived page")
    };

    let first = write(vec![BuiltDoc::derived(
        "code/features.md",
        "features v1\n".to_string(),
        "contract-1".to_string(),
    )]);
    assert!(first.iter().any(|p| p == "code/features.md"));

    // Same key (same contract) — even with different bytes, the page is unchanged
    // for incremental purposes and must not rewrite.
    let second = write(vec![BuiltDoc::derived(
        "code/features.md",
        "features v1 (regenerated)\n".to_string(),
        "contract-1".to_string(),
    )]);
    assert!(
        second.is_empty(),
        "an unchanged invalidation key must skip the rewrite: {second:?}"
    );

    // Changed key (the contract surface moved) — the page rewrites.
    let third = write(vec![BuiltDoc::derived(
        "code/features.md",
        "features v2\n".to_string(),
        "contract-2".to_string(),
    )]);
    assert!(third.iter().any(|p| p == "code/features.md"));
}

#[test]
fn since_scopes_regeneration_to_changed_files_and_preserves_the_rest() {
    let project = tempfile::tempdir().expect("project");
    let out_dir = project.path().join("codewiki");
    write_source(project.path(), "alpha/a.rs", "pub fn a_fn() {}\n");
    write_source(project.path(), "beta/b.rs", "pub fn b_fn() {}\n");

    let input = CodewikiInput {
        leading_chunks: BTreeMap::new(),
        files: vec!["alpha/a.rs".to_string(), "beta/b.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol("alpha/a.rs", "a_fn", "function", 1, "pub fn a_fn()"),
            test_symbol("beta/b.rs", "b_fn", "function", 1, "pub fn b_fn()"),
        ],
    };

    run_generate(&input, None, project.path(), &out_dir, None);

    // Both files change on disk, but git only reports alpha/a.rs in the diff.
    write_source(
        project.path(),
        "alpha/a.rs",
        "pub fn a_fn() {\n    let _ = 1;\n}\n",
    );
    write_source(
        project.path(),
        "beta/b.rs",
        "pub fn b_fn() {\n    let _ = 2;\n}\n",
    );
    let since = BTreeSet::from(["alpha/a.rs".to_string()]);

    let changed = run_generate(&input, None, project.path(), &out_dir, Some(since));
    assert!(
        changed.iter().any(|p| p == "code/files/alpha/a.rs.md"),
        "the in-diff file must rebuild: {changed:?}"
    );
    assert!(
        !changed.iter().any(|p| p == "code/files/beta/b.rs.md"),
        "--since must leave a file outside the diff untouched even if it changed on disk: {changed:?}"
    );

    // Out-of-scope page and its meta survive.
    assert!(out_dir.join("code/files/beta/b.rs.md").exists());
    let meta = std::fs::read_to_string(out_dir.join("_meta/codewiki.json")).expect("read meta");
    let meta: serde_json::Value = serde_json::from_str(&meta).expect("parse meta");
    assert!(meta["docs"].get("code/files/beta/b.rs.md").is_some());
}

#[test]
fn git_changed_files_lists_diff_against_ref() {
    let project = tempfile::tempdir().expect("project");
    let root = project.path();
    let git = |args: &[&str]| {
        let status = std::process::Command::new("git")
            .arg("-C")
            .arg(root)
            .args(args)
            .status()
            .expect("run git");
        assert!(status.success(), "git {args:?} failed");
    };
    git(&["init", "-q"]);
    git(&["config", "user.email", "test@example.com"]);
    git(&["config", "user.name", "Test"]);
    std::fs::write(root.join("a.rs"), "fn a() {}\n").expect("write a");
    std::fs::write(root.join("b.rs"), "fn b() {}\n").expect("write b");
    git(&["add", "."]);
    git(&["commit", "-q", "-m", "base"]);

    std::fs::write(root.join("a.rs"), "fn a() { let _ = 1; }\n").expect("edit a");
    let changed = git_changed_files(root, "HEAD").expect("git diff");
    assert!(
        changed.contains("a.rs"),
        "changed set should include a.rs: {changed:?}"
    );
    assert!(
        !changed.contains("b.rs"),
        "unchanged b.rs must not appear: {changed:?}"
    );

    // A bad ref fails loudly rather than silently degrading to a full scan.
    assert!(git_changed_files(root, "no-such-ref").is_err());
}

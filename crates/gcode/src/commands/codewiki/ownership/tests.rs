use super::*;
use std::collections::{BTreeMap, HashMap};
use std::path::Path;
use std::process::Command;
use std::time::Duration;

#[test]
fn codewiki_ownership_codeowners_only_maps_declared_owners() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src/api")).expect("create src/api");
    std::fs::write(project.path().join("CODEOWNERS"), "/src/api/ @platform\n")
        .expect("write CODEOWNERS");
    std::fs::write(project.path().join("src/api/mod.rs"), "pub fn api() {}\n")
        .expect("write source");

    let mut meta = OwnershipMeta::default();
    let doc = build_ownership_doc(
        project.path(),
        &["src/api/mod.rs".to_string()],
        &modules([("src/api/mod.rs", "src/api")]),
        &mut meta,
        OwnershipOptions {
            blame_file_cap: 10,
            blame_timeout: Duration::from_secs(10),
        },
    )
    .expect("ownership doc");

    assert!(doc.contains("type: code_ownership"));
    assert!(doc.contains("degraded: true"));
    assert!(doc.contains("git_blame_unavailable"));
    assert!(doc.contains("Module: [[code/modules/src/api|src/api]]"));
    assert!(doc.contains("Declared owners: @platform"));
    assert!(doc.contains("src/api/mod.rs"));
}

#[test]
fn codewiki_ownership_derives_top_committers_from_git_blame() {
    let project = git_project_with_history();
    let mut meta = OwnershipMeta::default();

    let doc = build_ownership_doc(
        project.path(),
        &["src/lib.rs".to_string()],
        &modules([("src/lib.rs", "src")]),
        &mut meta,
        OwnershipOptions {
            blame_file_cap: 10,
            blame_timeout: Duration::from_secs(10),
        },
    )
    .expect("ownership doc");

    assert!(doc.contains("Top contributors: Alice"));
    assert!(doc.contains("Bob"));
    assert!(!doc.contains("git_blame_unavailable"));
    assert_eq!(meta.files.len(), 1);
    let serialized = serde_json::to_string(&meta).expect("serialize ownership meta");
    assert!(serialized.contains("contributor_id"));
    assert!(!serialized.contains("alice@example.com"));
    assert!(!serialized.contains("bob@example.com"));
}

#[test]
fn codewiki_ownership_requires_cached_contributor_ids() {
    let error = serde_json::from_str::<OwnershipMeta>(
        r#"{
"files": {
"src/lib.rs": {
                  "content_hash": "hash",
                  "contributors": [
                    {"name": "Alice", "email": "alice@example.test", "lines": 4},
                    {"name": "Bob", "email": "bob@example.test", "lines": 2}
                  ]
                }
}
}"#,
    )
    .expect_err("legacy ownership meta without contributor_id is rejected");

    assert!(error.to_string().contains("missing field `contributor_id`"));
}

#[test]
fn codewiki_ownership_declared_owners_take_primary_precedence() {
    let project = git_project_with_history();
    std::fs::write(project.path().join("CODEOWNERS"), "src/lib.rs @declared\n")
        .expect("write CODEOWNERS");
    let mut meta = OwnershipMeta::default();

    let doc = build_ownership_doc(
        project.path(),
        &["src/lib.rs".to_string()],
        &modules([("src/lib.rs", "src")]),
        &mut meta,
        OwnershipOptions {
            blame_file_cap: 10,
            blame_timeout: Duration::from_secs(10),
        },
    )
    .expect("ownership doc");

    assert!(doc.contains("Declared owners: @declared"));
    assert!(doc.contains("Top contributors: Alice"));
    assert!(!doc.contains("Primary owners: Alice"));
}

#[test]
fn codewiki_ownership_without_sources_degrades_to_unknown() {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src")).expect("create src");
    std::fs::write(project.path().join("src/lib.rs"), "pub fn lib() {}\n").expect("write source");
    let mut meta = OwnershipMeta::default();

    let doc = build_ownership_doc(
        project.path(),
        &["src/lib.rs".to_string()],
        &modules([("src/lib.rs", "src")]),
        &mut meta,
        OwnershipOptions {
            blame_file_cap: 10,
            blame_timeout: Duration::from_secs(10),
        },
    )
    .expect("ownership doc");

    assert!(doc.contains("degraded: true"));
    assert!(doc.contains("codeowners_unavailable"));
    assert!(doc.contains("git_blame_unavailable"));
    assert!(doc.contains("Unknown ownership"));
}

#[test]
fn codewiki_ownership_file_cap_marks_partial() {
    let project = git_project_with_two_files();
    let mut meta = OwnershipMeta::default();

    let doc = build_ownership_doc(
        project.path(),
        &["src/a.rs".to_string(), "src/b.rs".to_string()],
        &modules([("src/a.rs", "src"), ("src/b.rs", "src")]),
        &mut meta,
        OwnershipOptions {
            blame_file_cap: 1,
            blame_timeout: Duration::from_secs(10),
        },
    )
    .expect("ownership doc");

    assert!(doc.contains("partial: true"));
    assert!(doc.contains("Ownership is partial"));
    assert_eq!(meta.files.len(), 1);
}

#[test]
fn codewiki_ownership_file_cap_counts_only_cache_misses() {
    let project = git_project_with_two_files();
    let cached_hash = content_hash(project.path(), "src/a.rs").expect("hash a");
    let cached_contributor = OwnershipContributor {
        contributor_id: "cached".to_string(),
        name: "Cached Owner".to_string(),
        email: None,
        lines: 1,
    };
    let mut meta = OwnershipMeta {
        files: BTreeMap::from([(
            "src/a.rs".to_string(),
            CachedBlameSummary {
                content_hash: cached_hash,
                contributors: vec![cached_contributor],
            },
        )]),
    };

    let doc = build_ownership_doc(
        project.path(),
        &["src/a.rs".to_string(), "src/b.rs".to_string()],
        &modules([("src/a.rs", "src"), ("src/b.rs", "src")]),
        &mut meta,
        OwnershipOptions {
            blame_file_cap: 1,
            blame_timeout: Duration::from_secs(10),
        },
    )
    .expect("ownership doc");

    assert!(!doc.contains("partial: true"));
    assert!(!doc.contains("Ownership is partial"));
    assert!(meta.files.contains_key("src/a.rs"));
    assert!(meta.files.contains_key("src/b.rs"));
}

#[test]
fn codewiki_ownership_blame_error_marks_partial_without_caching() {
    let project = git_project_with_history();
    std::fs::write(
        project.path().join("src/untracked.rs"),
        "pub fn untracked() {}\n",
    )
    .expect("write untracked source");
    let mut meta = OwnershipMeta::default();

    let doc = build_ownership_doc(
        project.path(),
        &["src/untracked.rs".to_string()],
        &modules([("src/untracked.rs", "src")]),
        &mut meta,
        OwnershipOptions {
            blame_file_cap: 10,
            blame_timeout: Duration::from_secs(10),
        },
    )
    .expect("ownership doc");

    assert!(doc.contains("partial: true"));
    assert!(doc.contains("git_blame_errors"));
    assert!(meta.files.is_empty());
}

fn modules<const N: usize>(items: [(&str, &str); N]) -> HashMap<String, String> {
    items
        .into_iter()
        .map(|(file, module)| (file.to_string(), module.to_string()))
        .collect()
}

fn git_project_with_history() -> tempfile::TempDir {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src")).expect("create src");
    git(project.path(), &["init"]);
    std::fs::write(
        project.path().join("src/lib.rs"),
        "pub fn one() {}\npub fn two() {}\n",
    )
    .expect("write source");
    git(project.path(), &["add", "src/lib.rs"]);
    git_author(project.path(), "Alice", "alice@example.test", "initial");
    std::fs::write(
        project.path().join("src/lib.rs"),
        "pub fn one() {}\npub fn two_changed() {}\n",
    )
    .expect("rewrite source");
    git(project.path(), &["add", "src/lib.rs"]);
    git_author(project.path(), "Bob", "bob@example.test", "update");
    project
}

fn git_project_with_two_files() -> tempfile::TempDir {
    let project = tempfile::tempdir().expect("project tempdir");
    std::fs::create_dir_all(project.path().join("src")).expect("create src");
    git(project.path(), &["init"]);
    std::fs::write(project.path().join("src/a.rs"), "pub fn a() {}\n").expect("write a");
    std::fs::write(project.path().join("src/b.rs"), "pub fn b() {}\n").expect("write b");
    git(project.path(), &["add", "src/a.rs", "src/b.rs"]);
    git_author(project.path(), "Alice", "alice@example.test", "initial");
    project
}

fn git_author(repo: &Path, name: &str, email: &str, message: &str) {
    let status = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args([
            "-c",
            &format!("user.name={name}"),
            "-c",
            &format!("user.email={email}"),
            "commit",
            "-m",
            message,
        ])
        .status()
        .expect("run git commit");
    assert!(status.success(), "git commit failed");
}

fn git(repo: &Path, args: &[&str]) {
    let status = Command::new("git")
        .arg("-C")
        .arg(repo)
        .args(args)
        .status()
        .expect("run git");
    assert!(status.success(), "git {args:?} failed");
}

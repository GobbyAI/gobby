use std::collections::HashMap;
use std::path::{Path, PathBuf};

use super::super::overlay::{
    IndexedFileState, OverlayReconcileAction, overlay_reconcile_action,
    overlay_reconcile_candidates,
};
use super::super::types::IndexRequest;
use super::fixtures::{git, write_file};
use crate::visibility;

fn incremental_request(root: &Path) -> IndexRequest {
    IndexRequest {
        project_root: root.to_path_buf(),
        path_filter: None,
        explicit_files: Vec::new(),
        full: false,
        require_cpp_semantics: false,
        sync_projections: false,
    }
}

#[test]
fn incremental_overlay_candidates_cover_divergence_from_the_parent_checkout() {
    let base = tempfile::tempdir().expect("create temp base");
    let hooks = base.path().join("hooks");
    std::fs::create_dir_all(&hooks).expect("create hooks dir");
    let parent = base.path().join("parent");
    std::fs::create_dir_all(&parent).expect("create parent root");
    let overlay = base.path().join("overlay");

    for rel in [
        "same.rs",
        "branch_changed.rs",
        "parent_changed.rs",
        "parent_dirty.rs",
    ] {
        write_file(&parent, rel, b"pub fn seed() {}\n");
    }
    // Indexed in the parent but invisible to git in both trees: a generated
    // vault the parent gitignores and the worktree never checks out.
    write_file(&parent, ".gitignore", b"/generated/\n");
    write_file(&parent, "generated/page.md", b"# generated\n");
    git(&parent, &hooks, &["init", "-q"]);
    git(&parent, &hooks, &["add", "."]);
    git(&parent, &hooks, &["commit", "-q", "-m", "seed"]);
    git(
        &parent,
        &hooks,
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            "wt",
            overlay.to_str().expect("utf-8 overlay path"),
        ],
    );

    // Diverged only by a commit on the overlay branch, plus an untracked file.
    write_file(&overlay, "branch_changed.rs", b"pub fn branch() {}\n");
    git(&overlay, &hooks, &["commit", "-q", "-am", "branch change"]);
    write_file(&overlay, "overlay_untracked.rs", b"pub fn untracked() {}\n");
    // Diverged on the parent side: a commit after the overlay branched, a
    // dirty working-tree edit, and an untracked file.
    write_file(&parent, "parent_changed.rs", b"pub fn moved_ahead() {}\n");
    git(
        &parent,
        &hooks,
        &["commit", "-q", "-am", "parent moved ahead"],
    );
    write_file(&parent, "parent_dirty.rs", b"pub fn dirty() {}\n");
    write_file(
        &parent,
        "parent_untracked.rs",
        b"pub fn parent_untracked() {}\n",
    );

    let no_paths: HashMap<String, PathBuf> = HashMap::new();
    let no_states: HashMap<String, IndexedFileState> = HashMap::new();
    // `same.rs` exists in the worktree and is not a candidate; the generated
    // page is missing there and has no overlay row, so it must be reconciled.
    let parent_files: HashMap<String, IndexedFileState> = ["same.rs", "generated/page.md"]
        .into_iter()
        .map(|rel| {
            (
                rel.to_string(),
                IndexedFileState {
                    content_hash: "parent-hash".to_string(),
                    language: "text".to_string(),
                },
            )
        })
        .collect();
    let rels = overlay_reconcile_candidates(
        &incremental_request(&overlay),
        &overlay,
        &parent,
        &no_paths,
        &no_paths,
        &parent_files,
        &no_states,
    );

    assert_eq!(
        rels,
        vec![
            "branch_changed.rs",
            "generated/page.md",
            "overlay_untracked.rs",
            "parent_changed.rs",
            "parent_dirty.rs",
            "parent_untracked.rs",
        ]
    );
}

#[test]
fn incremental_overlay_candidates_fall_back_to_discovery_without_git() {
    let base = tempfile::tempdir().expect("create temp base");
    let parent = base.path().join("parent");
    let overlay = base.path().join("overlay");
    std::fs::create_dir_all(&parent).expect("create parent root");
    std::fs::create_dir_all(&overlay).expect("create overlay root");

    let ast_by_rel = HashMap::from([("src/lib.rs".to_string(), overlay.join("src/lib.rs"))]);
    let no_paths: HashMap<String, PathBuf> = HashMap::new();
    let parent_files = HashMap::from([(
        "src/parent_only.rs".to_string(),
        IndexedFileState {
            content_hash: "parent-hash".to_string(),
            language: "rust".to_string(),
        },
    )]);
    let no_states: HashMap<String, IndexedFileState> = HashMap::new();
    let rels = overlay_reconcile_candidates(
        &incremental_request(&overlay),
        &overlay,
        &parent,
        &ast_by_rel,
        &no_paths,
        &parent_files,
        &no_states,
    );

    assert_eq!(rels, vec!["src/lib.rs", "src/parent_only.rs"]);
}

#[test]
fn overlay_reconciliation_actions_cover_inherit_shadow_add_delete() {
    let parent = IndexedFileState {
        content_hash: "parent-hash".to_string(),
        language: "rust".to_string(),
    };
    let overlay = IndexedFileState {
        content_hash: "overlay-hash".to_string(),
        language: "rust".to_string(),
    };
    let tombstone = IndexedFileState {
        content_hash: visibility::TOMBSTONE_HASH.to_string(),
        language: visibility::TOMBSTONE_LANGUAGE.to_string(),
    };

    assert_eq!(
        overlay_reconcile_action(
            true,
            Some("parent-hash"),
            Some(&parent),
            Some(&overlay),
            true
        ),
        OverlayReconcileAction::Inherit
    );
    assert_eq!(
        overlay_reconcile_action(
            true,
            Some("edited-hash"),
            Some(&parent),
            Some(&overlay),
            true
        ),
        OverlayReconcileAction::Index
    );
    assert_eq!(
        overlay_reconcile_action(true, Some("added-hash"), None, None, true),
        OverlayReconcileAction::Index
    );
    assert_eq!(
        overlay_reconcile_action(false, None, Some(&parent), None, true),
        OverlayReconcileAction::Tombstone
    );
    assert_eq!(
        overlay_reconcile_action(false, None, Some(&parent), Some(&tombstone), true),
        OverlayReconcileAction::Skip
    );
    assert_eq!(
        overlay_reconcile_action(false, None, None, Some(&overlay), true),
        OverlayReconcileAction::DeleteOverlay
    );
}

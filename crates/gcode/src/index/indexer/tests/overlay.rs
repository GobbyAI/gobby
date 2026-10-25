use std::collections::HashMap;
use std::path::{Path, PathBuf};

use super::super::overlay::{
    IndexedFileState, OverlayReconcileAction, explicit_overlay_file_maps, overlay_reconcile_action,
    overlay_reconcile_candidates, write_tombstone,
};
use super::super::types::IndexRequest;
use super::super::{IndexOptions, index_files};
use super::fixtures::{git, git_output, write_file, write_isolation_marker};
use super::serial_db::{
    cleanup_summary_preservation_project, connect_summary_preservation_test_db, machine_context,
    test_symbol, unique_test_uuid, write_postgres_parsed_file_facts_with_root,
};
use crate::config::ProjectIndexScope;
use crate::db;
use crate::index::hasher;
use crate::visibility;

/// Deletes the pins and rows a reconciliation test wrote. Pins reference the
/// parent's content versions, so they go before the projects they point at.
fn clean_overlay_projects(database_url: &str, overlay: &str, parent: &str) -> anyhow::Result<()> {
    let mut conn = db::connect_readwrite(database_url)?;
    for project in [overlay, parent] {
        conn.execute(
            "DELETE FROM code_overlay_pins WHERE overlay_project_id = $1 OR source_project_id = $1",
            &[&db::id_param(project)?],
        )?;
        cleanup_summary_preservation_project(&mut conn, project)?;
    }
    Ok(())
}

struct OverlayRowsCleanup {
    database_url: String,
    overlay: String,
    parent: String,
}

impl Drop for OverlayRowsCleanup {
    fn drop(&mut self) {
        let _ = clean_overlay_projects(&self.database_url, &self.overlay, &self.parent);
    }
}

/// Writes `source` into the parent checkout, indexes it as the parent's current
/// version of `rel`, and returns that content hash.
fn index_parent_version(
    conn: &mut postgres::Client,
    parent: &str,
    root: &Path,
    rel: &str,
    source: &[u8],
    name: &str,
) -> String {
    write_file(root, rel, source);
    let hash = hasher::file_content_hash(&root.join(rel)).expect("hash parent version");
    write_postgres_parsed_file_facts_with_root(
        conn,
        parent,
        root,
        rel,
        &hash,
        source,
        vec![test_symbol(parent, rel, &hash, name, 7, &hash)],
    );
    hash
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn owned_rows_leave_divergence_without_discovery() {
    let (mut conn, database_url) = connect_summary_preservation_test_db();
    let base = tempfile::tempdir().expect("create temp base");
    let hooks = base.path().join("hooks");
    std::fs::create_dir_all(&hooks).expect("create hooks dir");
    let parent_root = base.path().join("parent");
    std::fs::create_dir_all(&parent_root).expect("create parent root");
    let overlay_root = base.path().join("overlay");
    let dirty = "src/dirty.rs";
    let untracked = "src/untracked.rs";
    let reappear = "src/reappear.rs";

    // Both checkouts hold the pinned content of `dirty` and `reappear`, so the
    // overlay tree is clean against its base.
    write_file(&parent_root, dirty, b"pub fn pinned_dirty() {}\n");
    write_file(&parent_root, reappear, b"pub fn pinned_reappear() {}\n");
    // The overlay's isolation marker is untracked, so the parent ignores it.
    write_file(&parent_root, ".gitignore", b".gobby/\n");
    git(&parent_root, &hooks, &["init", "-q"]);
    git(&parent_root, &hooks, &["add", "."]);
    git(&parent_root, &hooks, &["commit", "-q", "-m", "seed"]);
    git(
        &parent_root,
        &hooks,
        &[
            "worktree",
            "add",
            "-q",
            "-b",
            "wt",
            overlay_root.to_str().expect("utf-8 overlay path"),
        ],
    );

    let machine_id = gobby_core::machine::read_local_machine_id().expect("read machine id");
    let parent_project_id = unique_test_uuid("gcode-owned-pin-parent");
    let overlay_project_id = unique_test_uuid("gcode-owned-pin-overlay");
    clean_overlay_projects(&database_url, &overlay_project_id, &parent_project_id)
        .expect("pre-clean overlay rows");
    let _cleanup = OverlayRowsCleanup {
        database_url: database_url.clone(),
        overlay: overlay_project_id.clone(),
        parent: parent_project_id.clone(),
    };
    // Setup only: the overlay's isolation marker names the seed its pins were
    // taken at, so the candidate-only path runs. Assertions below are unchanged.
    let base_sha = git_output(&overlay_root, &hooks, &["rev-parse", "HEAD"]);
    write_isolation_marker(&overlay_root, &parent_root, &parent_project_id, &base_sha);

    // The parent indexes the pinned content, the pins reference it, then the
    // parent moves on to newer content for both paths.
    let pinned_dirty = index_parent_version(
        &mut conn,
        &parent_project_id,
        &parent_root,
        dirty,
        b"pub fn pinned_dirty() {}\n",
        "pinned_dirty",
    );
    let pinned_reappear = index_parent_version(
        &mut conn,
        &parent_project_id,
        &parent_root,
        reappear,
        b"pub fn pinned_reappear() {}\n",
        "pinned_reappear",
    );
    for (rel, hash) in [
        (dirty, pinned_dirty.as_str()),
        (reappear, pinned_reappear.as_str()),
    ] {
        conn.execute(
            "INSERT INTO code_overlay_pins
                (machine_id, overlay_project_id, file_path, source_project_id, content_hash)
             VALUES ($1, $2, $3, $4, $5)",
            &[
                &db::id_param(&machine_id).expect("machine id"),
                &db::id_param(&overlay_project_id).expect("overlay id"),
                &rel,
                &db::id_param(&parent_project_id).expect("parent id"),
                &hash,
            ],
        )
        .expect("insert pin");
    }
    index_parent_version(
        &mut conn,
        &parent_project_id,
        &parent_root,
        dirty,
        b"pub fn parent_moved_dirty() {}\n",
        "parent_moved_dirty",
    );
    index_parent_version(
        &mut conn,
        &parent_project_id,
        &parent_root,
        reappear,
        b"pub fn parent_moved_reappear() {}\n",
        "parent_moved_reappear",
    );

    // The overlay's own rows: a dirty edit of the pinned file, an untracked
    // file it indexed and then deleted from disk, and a tombstone for the path
    // that reappeared with pinned content.
    let overlay_row = |conn: &mut postgres::Client, rel: &str, source: &[u8], name: &str| {
        let hash = hasher::content_hash(source);
        write_postgres_parsed_file_facts_with_root(
            conn,
            &overlay_project_id,
            &overlay_root,
            rel,
            &hash,
            source,
            vec![test_symbol(&overlay_project_id, rel, &hash, name, 7, &hash)],
        );
    };
    overlay_row(&mut conn, dirty, b"pub fn dirty_edit() {}\n", "dirty_edit");
    overlay_row(
        &mut conn,
        untracked,
        b"pub fn untracked_owned() {}\n",
        "untracked_owned",
    );
    write_tombstone(
        &mut conn,
        &machine_id,
        &overlay_project_id,
        &overlay_root,
        reappear,
    )
    .expect("tombstone the reappearing path");

    let mut ctx = machine_context(
        &database_url,
        &overlay_project_id,
        &overlay_root,
        &machine_id,
    );
    ctx.index_scope = ProjectIndexScope::Overlay {
        overlay_project_id: overlay_project_id.clone(),
        overlay_root: overlay_root.clone(),
        parent_project_id: parent_project_id.clone(),
        parent_root: parent_root.clone(),
    };
    let visible = |conn: &mut postgres::Client, rel: &str| -> Vec<String> {
        visibility::visible_symbols_for_file(conn, &ctx, rel)
            .expect("read visible symbols")
            .into_iter()
            .map(|symbol| format!("{}:{}", symbol.project_id, symbol.name))
            .collect()
    };
    assert_eq!(
        visible(&mut conn, dirty),
        vec![format!("{overlay_project_id}:dirty_edit")],
        "the overlay's dirty edit shadows the pin before reconciliation"
    );
    assert_eq!(
        visible(&mut conn, untracked),
        vec![format!("{overlay_project_id}:untracked_owned")],
    );
    assert_eq!(visible(&mut conn, reappear), Vec::<String>::new());

    index_files(
        incremental_request(&overlay_root),
        &ctx,
        IndexOptions::default(),
    )
    .expect("overlay reconciliation");

    // Every arm reports together, so one run shows which arms hold and which fail.
    let dirty_after = visible(&mut conn, dirty);
    let untracked_after = visible(&mut conn, untracked);
    let reappear_after = visible(&mut conn, reappear);
    let mut failures = Vec::new();
    if dirty_after != vec![format!("{parent_project_id}:pinned_dirty")] {
        failures.push(format!(
            "a dirty edit reverted to the pinned content inherits the pin and drops its owned row: got {dirty_after:?}"
        ));
    }
    if !untracked_after.is_empty() {
        failures.push(format!(
            "an owned untracked file deleted from disk leaves nothing behind: got {untracked_after:?}"
        ));
    }
    if reappear_after != vec![format!("{parent_project_id}:pinned_reappear")] {
        failures.push(format!(
            "a tombstoned path that reappears with pinned content inherits the pin: got {reappear_after:?}"
        ));
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

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
fn explicit_overlay_file_maps_route_only_requested_files() {
    let root = tempfile::tempdir().expect("create root");
    write_file(root.path(), "src/requested.py", b"VALUE = 1\n");
    write_file(root.path(), "src/unrequested.py", b"VALUE = 2\n");
    write_file(root.path(), "docs/requested.md", b"# Requested\n");

    let mut request = incremental_request(root.path());
    request.explicit_files = vec![
        PathBuf::from("src/requested.py"),
        PathBuf::from("docs/requested.md"),
    ];

    let (ast_by_rel, content_by_rel) = explicit_overlay_file_maps(&request, root.path(), &[], true);

    assert_eq!(
        ast_by_rel.keys().collect::<Vec<_>>(),
        vec![&"src/requested.py".to_string()]
    );
    assert_eq!(
        content_by_rel.keys().collect::<Vec<_>>(),
        vec![&"docs/requested.md".to_string()]
    );
    assert!(!ast_by_rel.contains_key("src/unrequested.py"));
}

#[test]
fn reconcile_uses_child_candidates_only() {
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
    write_file(&parent, ".gitignore", b"/generated/\n/.gobby/\n");
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

    // Setup only: the overlay's isolation marker names the seed its pins were
    // taken at, so the candidate-only path runs. Assertions below are unchanged.
    let base_sha = git_output(&overlay, &hooks, &["rev-parse", "HEAD"]);
    write_isolation_marker(&overlay, &parent, "parent-project", &base_sha);

    // The parent moves on after the overlay branched: a commit, a dirty edit,
    // and an untracked file. None of it is the overlay's divergence.
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
    let state = || IndexedFileState {
        content_hash: "parent-hash".to_string(),
        language: "text".to_string(),
        git_blob_id: None,
    };
    // Parent rows are never candidates on their own; only the overlay's
    // divergence and the rows it owns are reconciled.
    let parent_files: HashMap<String, IndexedFileState> = [
        "same.rs",
        "branch_changed.rs",
        "parent_changed.rs",
        "parent_dirty.rs",
        "generated/page.md",
    ]
    .into_iter()
    .map(|rel| (rel.to_string(), state()))
    .collect();
    let candidates = || {
        overlay_reconcile_candidates(
            &incremental_request(&overlay),
            &overlay,
            &no_paths,
            &no_paths,
            &parent_files,
            &no_states,
        )
    };

    // No child divergence: the parent's HEAD movement, dirty edit, untracked
    // file, and parent-only ignored row are all ignored.
    assert_eq!(candidates(), Vec::<String>::new());

    // A commit on the overlay branch is divergence from its base.
    write_file(&overlay, "branch_changed.rs", b"pub fn branch() {}\n");
    git(&overlay, &hooks, &["commit", "-q", "-am", "branch change"]);
    assert_eq!(candidates(), vec!["branch_changed.rs"]);

    // An untracked overlay file is divergence too.
    write_file(&overlay, "overlay_untracked.rs", b"pub fn untracked() {}\n");
    assert_eq!(
        candidates(),
        vec!["branch_changed.rs", "overlay_untracked.rs"]
    );
}

#[test]
fn reconcile_falls_back_to_full_discovery() {
    let base = tempfile::tempdir().expect("create temp base");
    let overlay = base.path().join("overlay");
    std::fs::create_dir_all(&overlay).expect("create overlay root");

    let ast_by_rel = HashMap::from([("src/lib.rs".to_string(), overlay.join("src/lib.rs"))]);
    let content_by_rel =
        HashMap::from([("docs/readme.md".to_string(), overlay.join("docs/readme.md"))]);
    let state = || IndexedFileState {
        content_hash: "hash".to_string(),
        language: "rust".to_string(),
        git_blob_id: None,
    };
    let parent_files = HashMap::from([("src/parent_only.rs".to_string(), state())]);
    let overlay_files = HashMap::from([("src/overlay_only.rs".to_string(), state())]);
    let every_source = vec![
        "docs/readme.md",
        "src/lib.rs",
        "src/overlay_only.rs",
        "src/parent_only.rs",
    ];

    // Full mode walks every source, whatever Git would report.
    let mut full = incremental_request(&overlay);
    full.full = true;
    let rels = overlay_reconcile_candidates(
        &full,
        &overlay,
        &ast_by_rel,
        &content_by_rel,
        &parent_files,
        &overlay_files,
    );
    assert_eq!(rels, every_source);

    // No isolation marker: the pin base is unknowable, so divergence is too and
    // every source is reconciled.
    let rels = overlay_reconcile_candidates(
        &incremental_request(&overlay),
        &overlay,
        &ast_by_rel,
        &content_by_rel,
        &parent_files,
        &overlay_files,
    );
    assert_eq!(rels, every_source);
}

#[test]
fn overlay_reconciliation_actions_cover_inherit_shadow_add_delete() {
    let parent = IndexedFileState {
        content_hash: "parent-hash".to_string(),
        language: "rust".to_string(),
        git_blob_id: None,
    };
    let overlay = IndexedFileState {
        content_hash: "overlay-hash".to_string(),
        language: "rust".to_string(),
        git_blob_id: None,
    };
    let tombstone = IndexedFileState {
        content_hash: visibility::TOMBSTONE_HASH.to_string(),
        language: visibility::TOMBSTONE_LANGUAGE.to_string(),
        git_blob_id: None,
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

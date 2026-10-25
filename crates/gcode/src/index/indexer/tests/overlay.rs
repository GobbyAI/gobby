use std::collections::HashMap;
use std::path::{Path, PathBuf};

use super::super::overlay::{
    IndexedFileState, OverlayReconcileAction, explicit_overlay_file_maps, overlay_reconcile_action,
    overlay_reconcile_candidates,
};
use super::super::types::IndexRequest;
use super::super::{IndexOptions, index_files};
use super::fixtures::{git, write_file};
use super::serial_db::{
    SummaryPreservationCleanup, cleanup_summary_preservation_project,
    connect_summary_preservation_test_db, seed_primary_checkout, test_uuid_param, unique_test_uuid,
};
use crate::config::{CodeVectorSettings, Context, ProjectIndexScope};
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
                    git_blob_id: None,
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
            git_blob_id: None,
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

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn overlay_index_indexes_a_missing_parent_first() {
    let (mut conn, database_url) = connect_summary_preservation_test_db();
    let base = tempfile::tempdir().expect("create temp base");
    let hooks = base.path().join("hooks");
    std::fs::create_dir_all(&hooks).expect("create hooks dir");
    let parent_root = base.path().join("parent");
    std::fs::create_dir_all(&parent_root).expect("create parent root");
    let overlay_root = base.path().join("overlay");
    let rel = "src/lib.rs";
    let unchanged = "src/unchanged.rs";
    write_file(&parent_root, rel, b"pub fn parent_only() {}\n");
    write_file(&parent_root, unchanged, b"pub fn unchanged() {}\n");
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
    write_file(&overlay_root, rel, b"pub fn overlay_only() {}\n");
    git(
        &overlay_root,
        &hooks,
        &["commit", "-q", "-am", "overlay change"],
    );

    let overlay_project_id = unique_test_uuid("gcode-overlay-missing-parent");
    let parent_project_id = unique_test_uuid("gcode-overlay-missing-parent-root");
    cleanup_summary_preservation_project(&mut conn, &overlay_project_id)
        .expect("pre-clean overlay rows");
    cleanup_summary_preservation_project(&mut conn, &parent_project_id)
        .expect("pre-clean parent rows");
    let _overlay_cleanup = SummaryPreservationCleanup {
        database_url: database_url.clone(),
        project_id: overlay_project_id.clone(),
    };
    let _parent_cleanup = SummaryPreservationCleanup {
        database_url: database_url.clone(),
        project_id: parent_project_id.clone(),
    };
    // The parent has a checkout on this machine but no file states yet.
    seed_primary_checkout(&mut conn, &parent_project_id, &parent_root)
        .expect("seed parent checkout");
    conn.execute(
        "INSERT INTO code_indexed_projects (id) VALUES ($1)",
        &[&test_uuid_param(&parent_project_id)],
    )
    .expect("seed parent project state row");

    // The unchanged parent file already has fully projected content, so the
    // parent-first pass must adopt it rather than re-parse it.
    let unchanged_hash = crate::index::hasher::file_content_hash(&parent_root.join(unchanged))
        .expect("hash unchanged parent file");
    let unchanged_file = crate::models::IndexedFile {
        id: crate::models::IndexedFile::make_id(&parent_project_id, unchanged, &unchanged_hash),
        project_id: parent_project_id.clone(),
        file_path: unchanged.to_string(),
        language: "rust".to_string(),
        content_hash: unchanged_hash,
        symbol_count: 41,
        byte_size: 4096,
        indexed_at: String::new(),
    };
    crate::index::api::upsert_file(&mut conn, &unchanged_file)
        .expect("seed fully projected parent content");
    conn.execute(
        "UPDATE code_indexed_files
         SET graph_synced = TRUE, vectors_synced = TRUE
         WHERE id = $1",
        &[&test_uuid_param(&unchanged_file.id)],
    )
    .expect("mark parent projections complete");

    let ctx = Context {
        database_url,
        project_root: overlay_root.clone(),
        project_id: overlay_project_id.clone(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        machine_id: gobby_core::machine::read_local_machine_id().expect("read machine id"),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: ProjectIndexScope::Overlay {
            overlay_project_id: overlay_project_id.clone(),
            overlay_root: overlay_root.clone(),
            parent_project_id: parent_project_id.clone(),
            parent_root: parent_root.clone(),
        },
    };
    index_files(
        IndexRequest {
            project_root: overlay_root.clone(),
            path_filter: None,
            explicit_files: Vec::new(),
            full: false,
            require_cpp_semantics: false,
            sync_projections: true,
        },
        &ctx,
        IndexOptions::default(),
    )
    .expect("overlay index indexes the missing parent first");

    let parent_uuid = test_uuid_param(&parent_project_id);
    let machine_uuid = test_uuid_param(&ctx.machine_id);
    let parent_states: Vec<String> = conn
        .query(
            "SELECT file_path FROM code_indexed_file_states
             WHERE machine_id = $1 AND project_id = $2
             ORDER BY file_path",
            &[&machine_uuid, &parent_uuid],
        )
        .expect("load parent file states")
        .iter()
        .map(|row| row.get(0))
        .collect();
    assert_eq!(
        parent_states,
        vec![rel.to_string(), unchanged.to_string()],
        "the parent is indexed on this machine before the overlay pass"
    );
    let adopted_symbols: i32 = conn
        .query_one(
            "SELECT symbol_count FROM code_indexed_files WHERE id = $1",
            &[&test_uuid_param(&unchanged_file.id)],
        )
        .expect("load adopted parent content")
        .get(0);
    assert_eq!(
        adopted_symbols, 41,
        "a fully projected unchanged parent file is adopted, not re-parsed"
    );
    let visible: Vec<String> = visibility::visible_symbols_for_file(&mut conn, &ctx, rel)
        .expect("read visible symbols")
        .into_iter()
        .map(|symbol| format!("{}:{}", symbol.project_id, symbol.name))
        .collect();
    assert_eq!(
        visible,
        vec![format!("{overlay_project_id}:overlay_only")],
        "the overlay serves its own diverged file after the parent-first pass"
    );
}

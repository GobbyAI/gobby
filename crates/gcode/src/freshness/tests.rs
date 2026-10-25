use super::*;
use crate::commands::grep::{GrepOptions, grep_repo};
use crate::models::CODE_INDEX_UUID_NAMESPACE;
use crate::output::Format;
use postgres::Client;
use std::path::Path;

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn overlay_served_hash_agrees_with_visibility_for_a_pinned_path() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let parent_root = tmp.path().join("parent");
    let overlay_root = tmp.path().join("overlay");
    let rel = "src/lib.rs";
    for root in [&parent_root, &overlay_root] {
        std::fs::create_dir_all(root.join("src")).expect("create src");
        std::fs::write(root.join(rel), b"pub fn parent_name() {}\n").expect("write source");
    }
    let parent_ctx =
        postgres_context_with_root(&test_project_id("gcode-pinned-fresh-parent"), &parent_root);
    let mut overlay_ctx = postgres_context_with_root(
        &test_project_id("gcode-pinned-fresh-overlay"),
        &overlay_root,
    );
    overlay_ctx.index_scope = crate::config::ProjectIndexScope::Overlay {
        overlay_project_id: overlay_ctx.project_id.clone(),
        overlay_root: overlay_root.clone(),
        parent_project_id: parent_ctx.project_id.clone(),
        parent_root: parent_root.clone(),
    };
    invalidate_test_project(&parent_ctx);
    invalidate_test_project(&overlay_ctx);
    full_index(&parent_ctx);

    // The overlay owns no row for lib.rs: the pin serves the parent's indexed
    // content, which is what the worktree holds.
    let pinned = crate::index::hasher::file_content_hash(&overlay_root.join(rel))
        .expect("hash pinned content");
    let mut conn = crate::db::connect_readwrite(&overlay_ctx.database_url).expect("connect");
    conn.execute(
        "INSERT INTO code_overlay_pins
            (machine_id, overlay_project_id, file_path, source_project_id, content_hash)
         VALUES ($1, $2, $3, $4, $5)",
        &[
            &crate::db::id_param(&overlay_ctx.machine_id).expect("machine id"),
            &crate::db::id_param(&overlay_ctx.project_id).expect("overlay id"),
            &rel,
            &crate::db::id_param(&parent_ctx.project_id).expect("parent id"),
            &pinned,
        ],
    )
    .expect("pin lib.rs");

    // The parent reindexes lib.rs with new content. The overlay still serves
    // the pinned content, so freshness must agree with visibility.
    std::fs::write(parent_root.join(rel), b"pub fn parent_moved() {}\n").expect("write parent");
    full_index(&parent_ctx);

    assert_eq!(
        visible_symbol_names(&overlay_ctx, rel),
        ["parent_name"],
        "visibility serves the pinned content"
    );
    assert_eq!(
        overlay_served_content_hash(&mut conn, &overlay_ctx, rel).expect("served hash"),
        Some(pinned.clone()),
        "explicit-file freshness must read the content visibility serves, not the parent's new row"
    );
    let status = ensure_fresh(
        &overlay_ctx,
        FreshnessScope::Files(vec![PathBuf::from(rel)]),
    )
    .expect("lib freshness status");
    assert_eq!(status, FreshnessStatus::Checked);
    assert_eq!(visible_symbol_names(&overlay_ctx, rel), ["parent_name"]);

    conn.execute(
        "DELETE FROM code_overlay_pins WHERE overlay_project_id = $1",
        &[&crate::db::id_param(&overlay_ctx.project_id).expect("overlay id")],
    )
    .expect("delete pin");
    invalidate_test_project(&overlay_ctx);
    invalidate_test_project(&parent_ctx);
}

fn context_for(root: &Path) -> Context {
    Context {
        database_url: "postgresql://localhost/gobby-test".to_string(),
        project_root: root.to_path_buf(),
        project_id: "proj".to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings::default(),
        machine_id: gobby_core::machine::read_local_machine_id().expect("read machine id"),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    }
}

fn symbol_hash(source: &[u8], start: usize, end: usize) -> String {
    hasher::symbol_content_hash(source, start, end).expect("symbol hash")
}

/// Stable uuid project id for a freshness-test label; the hub stores
/// project ids as native uuid, so labels cannot be bound directly.
fn test_project_id(label: &str) -> String {
    uuid::Uuid::new_v5(&crate::models::CODE_INDEX_UUID_NAMESPACE, label.as_bytes()).to_string()
}

fn postgres_test_context(project_id: &str) -> Context {
    let database_url = crate::test_env::postgres_test_database_url("freshness tests");
    db::connect_readwrite(&database_url).expect("connect freshness PostgreSQL test database");
    Context {
        database_url,
        project_root: std::path::PathBuf::from("/tmp/gcode-freshness-lock-test"),
        project_id: project_id.to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings::default(),
        machine_id: gobby_core::machine::read_local_machine_id().expect("read machine id"),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    }
}

fn postgres_context_with_root(project_id: &str, root: &Path) -> Context {
    let root = root
        .canonicalize()
        .expect("canonical freshness test checkout");
    let database_url = crate::test_env::postgres_test_database_url("freshness tests");
    let mut conn =
        db::connect_readwrite(&database_url).expect("connect freshness PostgreSQL test database");
    crate::test_env::seed_test_checkout(&mut conn, project_id, &root)
        .expect("register freshness test checkout");
    Context {
        database_url,
        project_root: root,
        project_id: project_id.to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings::default(),
        machine_id: gobby_core::machine::read_local_machine_id().expect("read machine id"),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    }
}

fn hold_project_lock(ctx: &Context) -> Client {
    let mut conn = db::connect_readwrite(&ctx.database_url).expect("connect test PostgreSQL hub");
    let key = crate::index_lock::project_lock_key(&ctx.project_id);
    conn.execute("SELECT pg_advisory_lock($1)", &[&key])
        .expect("hold project advisory lock");
    conn
}

fn set_mtime(path: &Path, time: SystemTime) {
    std::fs::File::options()
        .read(true)
        .write(true)
        .open(path)
        .expect("open file to set mtime")
        .set_modified(time)
        .expect("set mtime");
}

fn invalidate_test_project(ctx: &Context) {
    let mut conn = db::connect_readwrite(&ctx.database_url).expect("connect test PostgreSQL hub");
    crate::index::indexer::invalidate(&mut conn, &ctx.project_id, None)
        .expect("invalidate test project index");
}

fn full_index(ctx: &Context) {
    api::index_files(
        api::IndexRequest {
            project_root: ctx.project_root.clone(),
            path_filter: None,
            explicit_files: Vec::new(),
            full: true,
            require_cpp_semantics: false,
            sync_projections: false,
        },
        ctx,
        api::IndexOptions::default(),
    )
    .expect("full index of test project");
}

/// Move a project's recorded `last_indexed_at` into the past so a sibling
/// project indexed at NOW() reads as newer, without sleeping in the test.
fn backdate_project_index(ctx: &Context, seconds: f64) {
    let mut conn = db::connect_readwrite(&ctx.database_url).expect("connect test PostgreSQL hub");
    let machine_id =
        db::id_param(&gobby_core::machine::read_local_machine_id().expect("local machine id"))
            .expect("machine uuid");
    let project_id = db::id_param(&ctx.project_id).expect("project uuid");
    conn.execute(
        "UPDATE code_indexed_project_states
            SET last_indexed_at = last_indexed_at - make_interval(secs => $3)
          WHERE machine_id = $1 AND project_id = $2",
        &[&machine_id, &project_id, &seconds],
    )
    .expect("backdate project index");
}

/// Pins each named path to the parent's indexed version, the rows `gcode pin`
/// writes for a worktree that matches the parent. Inherited rows come only from
/// pins, so an overlay indexed without them inherits nothing.
fn pin_parent_versions(overlay: &Context, parent: &Context, paths: &[&str]) {
    let mut conn =
        db::connect_readwrite(&overlay.database_url).expect("connect test PostgreSQL hub");
    let machine_id = db::id_param(&overlay.machine_id).expect("machine uuid");
    let overlay_id = db::id_param(&overlay.project_id).expect("overlay uuid");
    let parent_id = db::id_param(&parent.project_id).expect("parent uuid");
    for &path in paths {
        conn.execute(
            "INSERT INTO code_overlay_pins
             (machine_id, overlay_project_id, file_path, source_project_id, content_hash)
             SELECT machine_id, $2, file_path, project_id, content_hash
               FROM code_indexed_file_states
              WHERE machine_id = $1 AND project_id = $3 AND file_path = $4",
            &[&machine_id, &overlay_id, &parent_id, &path],
        )
        .expect("pin parent version");
    }
}

fn tombstone_count(ctx: &Context) -> usize {
    let mut conn = db::connect_readwrite(&ctx.database_url).expect("connect test PostgreSQL hub");
    visibility::tombstone_count(&mut conn, ctx)
}

fn visible_symbol_names(ctx: &Context, file_path: &str) -> Vec<String> {
    let mut conn = db::connect_readwrite(&ctx.database_url).expect("connect test PostgreSQL hub");
    visibility::visible_symbols_for_file(&mut conn, ctx, file_path)
        .expect("read visible symbols")
        .into_iter()
        .map(|symbol| symbol.name)
        .collect()
}

#[test]
fn busy_warning_names_the_holder_when_one_was_identified() {
    let named = FreshnessStatus::SkippedBusy(Some("holder: backend pid 4242".to_string()))
        .busy_warning()
        .expect("a busy status must produce a warning");
    assert!(named.contains("index refresh already running"), "{named}");
    assert!(named.contains("holder: backend pid 4242"), "{named}");

    // An unlooked-up holder must read as plain contention rather than
    // implying the lookup ran and found nobody.
    let unnamed = FreshnessStatus::SkippedBusy(None)
        .busy_warning()
        .expect("a busy status must produce a warning");
    assert!(!unnamed.contains("holder"), "{unnamed}");

    assert_eq!(FreshnessStatus::Checked.busy_warning(), None);
    assert_eq!(
        FreshnessStatus::Degraded("disk full".to_string()).busy_warning(),
        None
    );
}

mod serial_db {
    use super::*;
    use std::net::{TcpListener, TcpStream};
    use std::sync::mpsc;
    use std::time::Duration;

    #[test]
    #[serial_test::serial(serial_db)]
    fn freshness_inflight_env_short_circuits_project_refresh() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let ctx = context_for(tmp.path());
        unsafe { std::env::set_var(INFLIGHT_ENV, "1") };
        let result = ensure_fresh(&ctx, FreshnessScope::Project);
        unsafe { std::env::remove_var(INFLIGHT_ENV) };

        assert_eq!(result.expect("freshness status"), FreshnessStatus::Checked);
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn busy_project_lock_skips_freshness_refresh() {
        let ctx = postgres_test_context(&test_project_id("gcode-freshness-busy"));
        let _holder = hold_project_lock(&ctx);

        let status = ensure_fresh(&ctx, FreshnessScope::Project).expect("freshness status");

        assert!(
            matches!(status, FreshnessStatus::SkippedBusy(_)),
            "a held project lock must skip the refresh, got {status:?}"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn file_scope_skips_busy_lock_then_refreshes_after_release() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        std::fs::create_dir_all(root.join("src")).expect("create src");
        let lib = root.join("src/lib.rs");
        std::fs::write(&lib, b"pub fn stale_name() {}\n").expect("write initial source");
        let ctx = postgres_context_with_root(&test_project_id("gcode-freshness-file-lock"), root);
        invalidate_test_project(&ctx);
        full_index(&ctx);
        assert_eq!(visible_symbol_names(&ctx, "src/lib.rs"), ["stale_name"]);

        std::fs::write(&lib, b"pub fn fresh_name() {}\n").expect("write dirty source");
        let holder = hold_project_lock(&ctx);
        let (done_tx, done_rx) = std::sync::mpsc::channel();
        let handle = std::thread::spawn(move || {
            let status = ensure_fresh(
                &ctx,
                FreshnessScope::Files(vec![PathBuf::from("src/lib.rs")]),
            );
            done_tx.send(()).expect("send freshness completion");
            (status, ctx)
        });

        // Allow connection/setup latency while keeping the lock held until
        // freshness finishes. The status proves the busy-lock behavior;
        // the timeout bounds a regression that waits for this holder.
        let completed_while_locked = done_rx
            .recv_timeout(std::time::Duration::from_secs(30))
            .is_ok();
        drop(holder);
        let (status, ctx) = handle.join().expect("freshness thread joins");
        assert!(matches!(
            status.expect("file freshness status"),
            FreshnessStatus::SkippedBusy(_)
        ));
        assert!(
            completed_while_locked,
            "file-scoped navigation must finish while the project lock is held"
        );
        assert_eq!(visible_symbol_names(&ctx, "src/lib.rs"), ["stale_name"]);
        assert_eq!(
            ensure_fresh(
                &ctx,
                FreshnessScope::Files(vec![PathBuf::from("src/lib.rs")])
            )
            .expect("refresh after writer releases lock"),
            FreshnessStatus::Checked
        );
        assert_eq!(visible_symbol_names(&ctx, "src/lib.rs"), ["fresh_name"]);
        invalidate_test_project(&ctx);
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn file_scope_indexes_untracked_path_and_removes_deleted_path() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        std::fs::create_dir_all(root.join("src")).expect("create src");
        let lib = root.join("src/lib.rs");
        std::fs::write(&lib, b"pub fn indexed_name() {}\n").expect("write indexed source");
        let ctx =
            postgres_context_with_root(&test_project_id("gcode-freshness-file-lifecycle"), root);
        invalidate_test_project(&ctx);
        full_index(&ctx);

        let untracked = root.join("src/untracked.rs");
        std::fs::write(&untracked, b"pub fn newly_requested() {}\n")
            .expect("write untracked source");
        let status = ensure_fresh(
            &ctx,
            FreshnessScope::Files(vec![PathBuf::from("src/untracked.rs")]),
        )
        .expect("untracked file freshness status");
        assert_eq!(status, FreshnessStatus::Checked);
        assert_eq!(
            visible_symbol_names(&ctx, "src/untracked.rs"),
            ["newly_requested"]
        );

        std::fs::remove_file(&lib).expect("delete indexed source");
        let status = ensure_fresh(
            &ctx,
            FreshnessScope::Files(vec![PathBuf::from("src/lib.rs")]),
        )
        .expect("deleted file freshness status");
        assert_eq!(status, FreshnessStatus::Checked);
        assert!(visible_symbol_names(&ctx, "src/lib.rs").is_empty());
        invalidate_test_project(&ctx);
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn file_scope_refreshes_and_tombstones_worktree_overlay_path() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let parent_root = tmp.path().join("parent");
        let overlay_root = tmp.path().join("overlay");
        for root in [&parent_root, &overlay_root] {
            std::fs::create_dir_all(root.join("src")).expect("create src");
            std::fs::write(root.join("src/lib.rs"), b"pub fn parent_name() {}\n")
                .expect("write source");
        }
        let parent_ctx = postgres_context_with_root(
            &test_project_id("gcode-freshness-file-overlay-parent"),
            &parent_root,
        );
        let mut overlay_ctx = postgres_context_with_root(
            &test_project_id("gcode-freshness-file-overlay"),
            &overlay_root,
        );
        overlay_ctx.index_scope = ProjectIndexScope::Overlay {
            overlay_project_id: overlay_ctx.project_id.clone(),
            overlay_root: overlay_root.clone(),
            parent_project_id: parent_ctx.project_id.clone(),
            parent_root: parent_root.clone(),
        };
        invalidate_test_project(&parent_ctx);
        invalidate_test_project(&overlay_ctx);
        full_index(&parent_ctx);
        pin_parent_versions(&overlay_ctx, &parent_ctx, &["src/lib.rs"]);
        full_index(&overlay_ctx);
        assert_eq!(
            visible_symbol_names(&overlay_ctx, "src/lib.rs"),
            ["parent_name"]
        );

        let overlay_lib = overlay_root.join("src/lib.rs");
        std::fs::write(&overlay_lib, b"pub fn overlay_name() {}\n").expect("write overlay source");
        let status = ensure_fresh(
            &overlay_ctx,
            FreshnessScope::Files(vec![PathBuf::from("src/lib.rs")]),
        )
        .expect("overlay file freshness status");
        assert_eq!(status, FreshnessStatus::Checked);
        assert_eq!(
            visible_symbol_names(&overlay_ctx, "src/lib.rs"),
            ["overlay_name"]
        );

        std::fs::remove_file(&overlay_lib).expect("delete overlay source");
        let status = ensure_fresh(
            &overlay_ctx,
            FreshnessScope::Files(vec![PathBuf::from("src/lib.rs")]),
        )
        .expect("deleted overlay freshness status");
        assert_eq!(status, FreshnessStatus::Checked);
        assert!(visible_symbol_names(&overlay_ctx, "src/lib.rs").is_empty());
        assert_eq!(tombstone_count(&overlay_ctx), 1);

        invalidate_test_project(&overlay_ctx);
        invalidate_test_project(&parent_ctx);
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn pre_gate_skips_lock_when_unchanged_and_trips_after_a_change() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path();
        std::fs::create_dir_all(root.join("src")).expect("create src");
        let lib = root.join("src/lib.rs");
        std::fs::write(&lib, b"fn main() {}\n").expect("write lib.rs");
        std::fs::write(root.join("README.md"), b"# Title\n").expect("write README");

        // Age the files well past the skew margin so a clean index leaves them
        // unambiguously older than last_indexed_at, regardless of host/hub skew.
        let aged = SystemTime::now() - std::time::Duration::from_secs(3600);
        set_mtime(&lib, aged);
        set_mtime(&root.join("README.md"), aged);

        let ctx = postgres_context_with_root(&test_project_id("gcode-freshness-pregate"), root);

        // Start clean, then index so code_indexed_projects.last_indexed_at = NOW().
        invalidate_test_project(&ctx);
        full_index(&ctx);

        // Nothing changed: the pre-gate must skip the advisory lock entirely,
        // even while another connection holds it, and report Checked. Without
        // the gate this would force SkippedBusy.
        let holder = hold_project_lock(&ctx);
        let status = ensure_fresh(&ctx, FreshnessScope::Project).expect("freshness status");
        assert_eq!(status, FreshnessStatus::Checked);

        // Touch a tracked file with a future mtime so the gate trips regardless
        // of skew; with the lock still held it reports SkippedBusy, proving it
        // took the lock path rather than skipping.
        set_mtime(
            &lib,
            SystemTime::now() + std::time::Duration::from_secs(3600),
        );
        let status = ensure_fresh(&ctx, FreshnessScope::Project).expect("freshness status");
        assert!(
            matches!(status, FreshnessStatus::SkippedBusy(_)),
            "the changed file must take the lock path, got {status:?}"
        );
        drop(holder);

        invalidate_test_project(&ctx);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn symbol_slice_check_uses_stored_byte_range_hash() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let source = b"fn before() {}\nfn target() {}\n";
        std::fs::write(tmp.path().join("lib.rs"), source).expect("write file");
        let ctx = context_for(tmp.path());
        let start = 15;
        let end = source.len();
        let sym = Symbol {
            id: uuid::Uuid::new_v5(&CODE_INDEX_UUID_NAMESPACE, b"sym").to_string(),
            project_id: "proj".to_string(),
            file_path: "lib.rs".to_string(),
            name: "target".to_string(),
            qualified_name: "target".to_string(),
            kind: "function".to_string(),
            language: "rust".to_string(),
            byte_start: start,
            byte_end: end,
            line_start: 2,
            line_end: 2,
            signature: None,
            docstring: None,
            parent_symbol_id: None,
            file_content_hash: crate::index::hasher::content_hash(source),
            content_hash: symbol_hash(source, start, end),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        };

        assert!(symbol_slice_is_current(&ctx, &sym));

        std::fs::write(
            tmp.path().join("lib.rs"),
            b"// shifted\nfn before() {}\nfn target() {}\n",
        )
        .expect("shift file");
        assert!(!symbol_slice_is_current(&ctx, &sym));
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn overlay_pre_gate_counts_inherited_paths_and_trips_on_parent_reindex_or_edits() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let parent_root = tmp.path().join("parent");
        let overlay_root = tmp.path().join("overlay");
        // Age every file well past the skew margin and the back-dating below.
        let aged = SystemTime::now() - std::time::Duration::from_secs(4 * 3600);
        for root in [&parent_root, &overlay_root] {
            std::fs::create_dir_all(root.join("src")).expect("create src");
            for (rel, body) in [("src/lib.rs", "fn main() {}\n"), ("README.md", "# Title\n")] {
                let path = root.join(rel);
                std::fs::write(&path, body).expect("write file");
                set_mtime(&path, aged);
            }
        }
        // Indexed in the parent only, like a generated vault the worktree
        // never checks out: the overlay tombstones it and then stays quiet.
        std::fs::create_dir_all(parent_root.join("generated")).expect("create generated");
        let generated = parent_root.join("generated/page.md");
        std::fs::write(&generated, "# generated\n").expect("write generated page");
        set_mtime(&generated, aged);
        let parent_ctx = postgres_context_with_root(
            &test_project_id("gcode-freshness-overlay-parent"),
            &parent_root,
        );
        let mut overlay_ctx =
            postgres_context_with_root(&test_project_id("gcode-freshness-overlay"), &overlay_root);
        overlay_ctx.index_scope = crate::config::ProjectIndexScope::Overlay {
            overlay_project_id: overlay_ctx.project_id.clone(),
            overlay_root: overlay_root.clone(),
            parent_project_id: parent_ctx.project_id.clone(),
            parent_root: parent_root.clone(),
        };
        invalidate_test_project(&parent_ctx);
        invalidate_test_project(&overlay_ctx);
        full_index(&parent_ctx);
        pin_parent_versions(
            &overlay_ctx,
            &parent_ctx,
            &["src/lib.rs", "README.md", "generated/page.md"],
        );
        full_index(&overlay_ctx);
        assert_eq!(tombstone_count(&overlay_ctx), 1);

        // Every worktree path is served through its pin and older than the
        // overlay's index: nothing to refresh, no lock taken.
        assert!(!project_needs_refresh(&overlay_ctx).expect("pre-gate"));

        // A parent index newer than the overlay's changes nothing the overlay
        // serves: its pins name the content, so the gate stays quiet.
        backdate_project_index(&overlay_ctx, 3600.0);
        assert!(!project_needs_refresh(&overlay_ctx).expect("pre-gate"));

        // Deleting a pinned file in the worktree trips the gate; once
        // the refresh tombstones it, the known-absent path is quiet again.
        let readme = overlay_root.join("README.md");
        std::fs::remove_file(&readme).expect("delete README");
        assert!(project_needs_refresh(&overlay_ctx).expect("pre-gate"));
        let status = ensure_fresh(&overlay_ctx, FreshnessScope::Project).expect("refresh");
        assert_eq!(status, FreshnessStatus::Checked);
        assert_eq!(tombstone_count(&overlay_ctx), 2);
        assert!(!project_needs_refresh(&overlay_ctx).expect("pre-gate"));

        // A tombstoned path that reappears reads as new.
        std::fs::write(&readme, b"# Back\n").expect("recreate README");
        assert!(project_needs_refresh(&overlay_ctx).expect("pre-gate"));

        invalidate_test_project(&overlay_ctx);
        invalidate_test_project(&parent_ctx);
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn refresh_closure_failure_yields_degraded() {
        // `ensure_fresh` maps an acquired lock whose refresh closure failed
        // onto Degraded; hub/lock errors never reach this helper because
        // they stay `Err` from `with_project_lock` / `project_needs_refresh`.
        let status = freshness_from_lock(IndexLockResult::Acquired(Err(
            "unreadable project root".to_string()
        )));
        assert_eq!(
            status,
            FreshnessStatus::Degraded("unreadable project root".to_string())
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    fn degraded_detail_keeps_the_sql_cause_of_a_hub_error() {
        let database_url = crate::test_env::postgres_test_database_url("freshness tests");
        let mut client = db::connect_readwrite(&database_url).expect("connect hub");
        let error = client
            .batch_execute("SELECT 1/0")
            .expect_err("division by zero fails on the hub");

        let detail = degraded_detail(&anyhow::Error::from(error));

        assert!(detail.contains("division by zero"), "{detail}");
    }

    /// Forward the first hub connection and answer nothing afterwards.
    ///
    /// A freshness refresh connects twice: once for the index locks, then
    /// once for the indexing pipeline. Silencing every connection after the
    /// first reproduces #22412 — the pipeline socket is ESTABLISHED and the
    /// startup handshake is never answered — while the lock connection
    /// keeps working, which is what put the project advisory lock at risk.
    fn spawn_silent_after_first_hub_relay(database_url: &str) -> u16 {
        let upstream = hub_address(database_url);
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind the hub relay");
        let port = listener.local_addr().expect("hub relay address").port();
        std::thread::spawn(move || {
            let mut forwarded = false;
            let mut silenced = Vec::new();
            for stream in listener.incoming() {
                let Ok(stream) = stream else { break };
                if forwarded {
                    // Accepted, never answered, and held open so the client
                    // waits on the handshake instead of reading EOF.
                    silenced.push(stream);
                    continue;
                }
                forwarded = true;
                let hub = TcpStream::connect(upstream.as_str())
                    .expect("hub relay connect to the test hub");
                pump(
                    stream.try_clone().expect("clone the client socket"),
                    hub.try_clone().expect("clone the hub socket"),
                );
                pump(hub, stream);
            }
        });
        port
    }

    fn pump(mut source: TcpStream, mut sink: TcpStream) {
        std::thread::spawn(move || {
            let _ = std::io::copy(&mut source, &mut sink);
            let _ = sink.shutdown(std::net::Shutdown::Write);
        });
    }

    /// `host:port` of a `postgresql://user:password@host:port/database` DSN.
    fn hub_address(database_url: &str) -> String {
        let (_credentials, rest) = database_url
            .split_once('@')
            .expect("the test DSN carries credentials");
        let (authority, _database) = rest
            .split_once('/')
            .expect("the test DSN carries a database name");
        match authority.split_once(':') {
            Some(_) => authority.to_string(),
            None => format!("{authority}:5432"),
        }
    }

    /// The same DSN pointed at the relay, with a one-second connect bound.
    fn relay_database_url(database_url: &str, port: u16) -> String {
        let (credentials, rest) = database_url
            .split_once('@')
            .expect("the test DSN carries credentials");
        let (_authority, database) = rest
            .split_once('/')
            .expect("the test DSN carries a database name");
        let separator = if database.contains('?') { '&' } else { '?' };
        format!(
            "{credentials}@127.0.0.1:{port}/{database}{separator}\
             sslmode=disable&connect_timeout=1"
        )
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn refresh_connect_failure_releases_project_lock() {
        let tmp = tempfile::tempdir().expect("tempdir");
        std::fs::write(tmp.path().join("lib.rs"), b"pub fn one() {}\n").expect("write source");
        let ctx = postgres_context_with_root(
            &test_project_id("gcode-freshness-lock-release"),
            tmp.path(),
        );
        let port = spawn_silent_after_first_hub_relay(&ctx.database_url);
        let relay_ctx = Context {
            database_url: relay_database_url(&ctx.database_url, port),
            ..ctx.clone()
        };
        let file = relay_ctx.project_root.join("lib.rs");

        // The refresh must give up on its silenced pipeline connection; a
        // hang here is the #22412 failure, so it is bounded, not awaited.
        let (sender, receiver) = mpsc::channel();
        std::thread::spawn(move || {
            let _ = sender.send(ensure_fresh(&relay_ctx, FreshnessScope::Files(vec![file])));
        });
        let status = receiver
            .recv_timeout(Duration::from_secs(20))
            .expect("the refresh must fail closed instead of holding the lock forever")
            .expect("a failed pipeline connect degrades the refresh");

        match &status {
            FreshnessStatus::Degraded(message) => assert!(
                message.contains("startup handshake"),
                "the degraded reason must name the unanswered handshake: {message}"
            ),
            other => panic!("a failed pipeline connect must degrade, got {other:?}"),
        }

        let competitor =
            index_lock::with_project_lock(&ctx, IndexLockPolicy::brief_freshness_try(), || Ok(()))
                .expect("competitor lock attempt");
        assert!(
            matches!(competitor, IndexLockResult::Acquired(())),
            "the failed refresh must leave the project lock free, got {competitor:?}"
        );
    }

    #[test]
    #[serial_test::serial(serial_db)]
    fn hub_connect_failure_still_errors() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let mut ctx = context_for(tmp.path());
        ctx.database_url = "postgresql://gobby@/gobby_freshness_unreachable?\
            host=/tmp/gobby-no-postgres-hub&sslmode=disable"
            .to_string();

        let error = ensure_fresh(&ctx, FreshnessScope::Project)
            .expect_err("hub unavailability must fail closed");
        assert!(!error.to_string().is_empty());
    }
}

fn project_state_updated_at(ctx: &Context) -> SystemTime {
    let mut conn = db::connect_readonly(&ctx.database_url).expect("connect");
    let machine_id = db::id_param(&ctx.machine_id).expect("machine id");
    let project_id = db::id_param(&ctx.project_id).expect("project id");
    conn.query_one(
        "SELECT updated_at FROM code_indexed_project_states
         WHERE machine_id = $1 AND project_id = $2",
        &[&machine_id, &project_id],
    )
    .expect("project state row")
    .try_get::<_, SystemTime>(0)
    .expect("updated_at")
}

fn aged_overlay_pair(label: &str) -> (tempfile::TempDir, Context, Context) {
    let tmp = tempfile::tempdir().expect("tempdir");
    let parent_root = tmp.path().join("parent");
    let overlay_root = tmp.path().join("overlay");
    // Age the files so both indexes start settled for the project-wide
    // freshness paths; the explicit-file gate compares content hashes only.
    let aged = SystemTime::now() - std::time::Duration::from_secs(3600);
    for root in [&parent_root, &overlay_root] {
        std::fs::create_dir_all(root.join("src")).expect("create src");
        for (file, source) in [
            ("src/lib.rs", "pub fn parent_name() {}\n"),
            ("src/other.rs", "pub fn other_name() {}\n"),
        ] {
            let path = root.join(file);
            std::fs::write(&path, source).expect("write source");
            set_mtime(&path, aged);
        }
    }
    let parent_ctx =
        postgres_context_with_root(&test_project_id(&format!("{label}-parent")), &parent_root);
    let mut overlay_ctx =
        postgres_context_with_root(&test_project_id(&format!("{label}-overlay")), &overlay_root);
    overlay_ctx.index_scope = ProjectIndexScope::Overlay {
        overlay_project_id: overlay_ctx.project_id.clone(),
        overlay_root: overlay_root.clone(),
        parent_project_id: parent_ctx.project_id.clone(),
        parent_root: parent_root.clone(),
    };
    invalidate_test_project(&parent_ctx);
    invalidate_test_project(&overlay_ctx);
    full_index(&parent_ctx);
    full_index(&overlay_ctx);
    (tmp, parent_ctx, overlay_ctx)
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn overlay_explicit_file_grep_skips_refresh_when_nothing_changed() {
    let (tmp, parent_ctx, overlay_ctx) = aged_overlay_pair("gcode-freshness-noop");
    let file = vec![PathBuf::from("src/lib.rs")];
    let watermark = project_state_updated_at(&overlay_ctx);

    // A clean explicit-file read leaves the overlay untouched on every call.
    for _ in 0..2 {
        let status = ensure_fresh(&overlay_ctx, FreshnessScope::Files(file.clone()))
            .expect("clean freshness status");
        assert_eq!(status, FreshnessStatus::Checked);
        assert_eq!(
            project_state_updated_at(&overlay_ctx),
            watermark,
            "a clean explicit-file read must not refresh the overlay"
        );
    }

    // An edited worktree file is refreshed.
    let overlay_lib = tmp.path().join("overlay/src/lib.rs");
    std::fs::write(&overlay_lib, b"pub fn overlay_name() {}\n").expect("write overlay source");
    let status = ensure_fresh(&overlay_ctx, FreshnessScope::Files(file.clone()))
        .expect("edited freshness status");
    assert_eq!(status, FreshnessStatus::Checked);
    assert_eq!(
        visible_symbol_names(&overlay_ctx, "src/lib.rs"),
        ["overlay_name"]
    );
    let refreshed = project_state_updated_at(&overlay_ctx);
    assert!(refreshed > watermark, "the edit must refresh the overlay");

    // The overlay now serves the edited content, so the next read is quiet.
    let status =
        ensure_fresh(&overlay_ctx, FreshnessScope::Files(file)).expect("settled freshness status");
    assert_eq!(status, FreshnessStatus::Checked);
    assert_eq!(project_state_updated_at(&overlay_ctx), refreshed);

    invalidate_test_project(&overlay_ctx);
    invalidate_test_project(&parent_ctx);
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn overlay_explicit_file_grep_refreshes_inherited_file_after_parent_reindex() {
    let (tmp, parent_ctx, overlay_ctx) = aged_overlay_pair("gcode-freshness-parent-reindex");

    // The main checkout rewrites lib.rs and reindexes; the worktree keeps the old text.
    std::fs::write(
        tmp.path().join("parent/src/lib.rs"),
        b"pub fn parent_moved() {}\n",
    )
    .expect("write parent source");
    full_index(&parent_ctx);

    // An explicit read of another file must not certify the inherited lib.rs.
    let other = FreshnessScope::Files(vec![PathBuf::from("src/other.rs")]);
    let status = ensure_fresh(&overlay_ctx, other).expect("other file freshness status");
    assert_eq!(status, FreshnessStatus::Checked);
    let lib = FreshnessScope::Files(vec![PathBuf::from("src/lib.rs")]);
    let status = ensure_fresh(&overlay_ctx, lib).expect("lib freshness status");
    assert_eq!(status, FreshnessStatus::Checked);
    assert_eq!(
        visible_symbol_names(&overlay_ctx, "src/lib.rs"),
        ["parent_name"],
        "the overlay must serve the worktree's lib.rs, not the parent's new rows"
    );

    invalidate_test_project(&overlay_ctx);
    invalidate_test_project(&parent_ctx);
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn overlay_explicit_file_grep_refreshes_each_file_edited_before_the_first_read() {
    let (tmp, parent_ctx, overlay_ctx) = aged_overlay_pair("gcode-freshness-two-edits");
    std::fs::write(
        tmp.path().join("overlay/src/lib.rs"),
        b"pub fn lib_edited() {}\n",
    )
    .expect("write lib edit");
    std::fs::write(
        tmp.path().join("overlay/src/other.rs"),
        b"pub fn other_edited() {}\n",
    )
    .expect("write other edit");
    // The gate compares each file's content hash, so no time has to pass
    // between the edits and the first read. A time-based gate would skip the
    // second file here and fail this test.

    for file in ["src/lib.rs", "src/other.rs"] {
        let status = ensure_fresh(
            &overlay_ctx,
            FreshnessScope::Files(vec![PathBuf::from(file)]),
        )
        .expect("explicit read freshness status");
        assert_eq!(status, FreshnessStatus::Checked);
    }
    assert_eq!(
        visible_symbol_names(&overlay_ctx, "src/lib.rs"),
        ["lib_edited"]
    );
    assert_eq!(
        visible_symbol_names(&overlay_ctx, "src/other.rs"),
        ["other_edited"],
        "the second edited file must refresh after the first read"
    );

    invalidate_test_project(&overlay_ctx);
    invalidate_test_project(&parent_ctx);
}

// #25123: a worktree overlay that merges a parent commit changing an indexed
// file must serve the merged content. Before the merge the overlay owns rows
// for the file; after a fast-forward its HEAD equals the parent's HEAD, and the
// parent index lags that HEAD, so git sees no divergence to reconcile.
const MERGE_SEED_SOURCE: &str =
    "pub fn current_body() -> u8 {\n    1\n}\n\npub fn caller() -> u8 {\n    current_body()\n}\n";
const MERGED_SOURCE: &str = "pub fn added_header() -> u8 {\n    0\n}\n\npub fn current_body() -> u8 {\n    2\n}\n\npub fn caller() -> u8 {\n    current_body()\n}\n\npub fn second_caller() -> u8 {\n    current_body()\n}\n";

fn git_in(dir: &Path, hooks: &Path, args: &[&str]) -> String {
    let output = std::process::Command::new("git")
        .arg("-C")
        .arg(dir)
        .args([
            "-c",
            "user.name=Gcode Test",
            "-c",
            "user.email=gcode@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
        ])
        .arg(format!("core.hooksPath={}", hooks.display()))
        .args(args)
        .output()
        .expect("run git");
    assert!(
        output.status.success(),
        "git {args:?} failed\nstderr:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

/// Parent indexed at the seed; worktree overlay indexed at the seed; the parent
/// commits `MERGED_SOURCE` without reindexing; the worktree fast-forwards onto it.
fn merged_worktree_pair(label: &str) -> (tempfile::TempDir, Context, Context) {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hooks = tmp.path().join("hooks");
    std::fs::create_dir_all(&hooks).expect("create hooks dir");
    let parent_root = tmp.path().join("parent");
    let overlay_root = tmp.path().join("overlay");
    let rel = "src/lib.rs";
    std::fs::create_dir_all(parent_root.join("src")).expect("create parent src");
    std::fs::write(parent_root.join(rel), MERGE_SEED_SOURCE).expect("write seed source");
    git_in(&parent_root, &hooks, &["init", "-q"]);
    git_in(&parent_root, &hooks, &["add", "."]);
    git_in(&parent_root, &hooks, &["commit", "-q", "-m", "seed"]);
    git_in(
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

    let parent_ctx =
        postgres_context_with_root(&test_project_id(&format!("{label}-parent")), &parent_root);
    let mut overlay_ctx =
        postgres_context_with_root(&test_project_id(&format!("{label}-overlay")), &overlay_root);
    overlay_ctx.index_scope = ProjectIndexScope::Overlay {
        overlay_project_id: overlay_ctx.project_id.clone(),
        overlay_root: overlay_root.clone(),
        parent_project_id: parent_ctx.project_id.clone(),
        parent_root: parent_root.clone(),
    };
    let seed_sha = git_in(&parent_root, &hooks, &["rev-parse", "HEAD"]);
    invalidate_test_project(&parent_ctx);
    invalidate_test_project(&overlay_ctx);
    full_index(&parent_ctx);
    let parent_path = parent_root.canonicalize().expect("canonical parent root");
    let parent_str = parent_path.to_str().expect("utf-8 parent path").to_string();
    // The isolation marker names the clean base the worktree was created from.
    let write_marker = |base: &str| {
        let marker = serde_json::json!({
            "parent_project_path": parent_str,
            "parent_project_id": parent_ctx.project_id,
            "base_commit": base,
            "ancestor_checkout_path": parent_str,
            "ancestor_checkout_identity": parent_ctx.project_id,
        });
        std::fs::create_dir_all(overlay_root.join(".gobby")).expect("create gobby dir");
        std::fs::write(
            overlay_root.join(".gobby").join("isolation.json"),
            marker.to_string(),
        )
        .expect("write isolation marker");
    };
    write_marker(&seed_sha);
    pin_parent_versions(&overlay_ctx, &parent_ctx, &[rel]);

    // The parent commits the change; its index does not follow the new HEAD.
    std::fs::write(parent_root.join(rel), MERGED_SOURCE).expect("write merged source");
    git_in(
        &parent_root,
        &hooks,
        &["commit", "-q", "-am", "parent change"],
    );
    full_index(&parent_ctx);
    // The worktree fast-forwards onto the parent's commit.
    let merged_sha = git_in(&parent_root, &hooks, &["rev-parse", "HEAD"]);
    git_in(
        &overlay_root,
        &hooks,
        &["merge", "-q", "--ff-only", &merged_sha],
    );
    // The merged file's mtime must be newer than the overlay's index time.
    backdate_project_index(&overlay_ctx, 3600.0);
    (tmp, parent_ctx, overlay_ctx)
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn merged_worktree_symbol_at_serves_the_merged_body() {
    let (tmp, parent_ctx, overlay_ctx) = merged_worktree_pair("gcode-merge-symbol");
    let rel = "src/lib.rs";
    // symbol-at checks freshness for its file before it reads the symbol rows.
    let status = ensure_fresh(
        &overlay_ctx,
        FreshnessScope::Files(vec![PathBuf::from(rel)]),
    )
    .expect("symbol-at freshness status");
    assert_eq!(status, FreshnessStatus::Checked);

    let source = std::fs::read(tmp.path().join("overlay").join(rel)).expect("read merged source");
    let mut conn = db::connect_readonly(&overlay_ctx.database_url).expect("connect readonly");
    let symbols = visibility::visible_symbols_for_file(&mut conn, &overlay_ctx, rel)
        .expect("visible symbols");
    let body = symbols
        .iter()
        .find(|symbol| symbol.name == "current_body")
        .expect("current_body is visible");
    let snippet = String::from_utf8_lossy(
        source
            .get(body.byte_start..body.byte_end)
            .unwrap_or_default(),
    )
    .into_owned();
    assert_eq!(
        snippet.trim_end(),
        "pub fn current_body() -> u8 {\n    2\n}",
        "symbol-at must slice the merged file with the rows' current byte range"
    );

    invalidate_test_project(&overlay_ctx);
    invalidate_test_project(&parent_ctx);
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn merged_worktree_grep_finds_every_current_caller() {
    let (_tmp, parent_ctx, overlay_ctx) = merged_worktree_pair("gcode-merge-grep");
    // An unscoped grep checks freshness for the whole project.
    let status =
        ensure_fresh(&overlay_ctx, FreshnessScope::Project).expect("grep freshness status");
    assert_eq!(status, FreshnessStatus::Checked);

    assert_grep_finds_current_callers(&overlay_ctx);

    invalidate_test_project(&overlay_ctx);
    invalidate_test_project(&parent_ctx);
}

/// `grep -w` over the overlay must name every current line that mentions `current_body`.
fn assert_grep_finds_current_callers(overlay_ctx: &Context) {
    let mut conn = db::connect_readonly(&overlay_ctx.database_url).expect("connect readonly");
    let options = GrepOptions {
        pattern: "current_body",
        paths: &[],
        globs: &[],
        fixed_strings: true,
        ignore_case: false,
        word: true,
        context: None,
        before_context: None,
        after_context: None,
        max_count: None,
        offset: 0,
        token_budget: None,
        files_with_matches: false,
        format: Format::Json,
    };
    let result = grep_repo(overlay_ctx, &mut conn, &options).expect("grep merged worktree");
    let mut lines: Vec<usize> = result
        .matches
        .iter()
        .filter(|found| found.path == "src/lib.rs")
        .map(|found| found.line)
        .collect();
    lines.sort_unstable();
    let expected: Vec<usize> = MERGED_SOURCE
        .lines()
        .enumerate()
        .filter(|(_, line)| line.contains("current_body"))
        .map(|(index, _)| index + 1)
        .collect();
    assert_eq!(
        lines, expected,
        "grep -w must return every current line naming current_body, including the new caller"
    );
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn merged_worktree_grep_survives_a_filtered_overlay_index() {
    let (tmp, parent_ctx, overlay_ctx) = merged_worktree_pair("gcode-merge-filtered");
    let overlay_root = tmp.path().join("overlay");
    // The merged file and an unrelated file are an hour old, well outside the
    // mtime skew margin the freshness pre-gate allows.
    let an_hour_ago = std::time::SystemTime::now() - std::time::Duration::from_secs(3600);
    let other = overlay_root.join("src/other.rs");
    std::fs::write(&other, "pub fn other() {}\n").expect("write unrelated file");
    for path in [overlay_root.join("src/lib.rs"), other] {
        std::fs::File::options()
            .write(true)
            .open(&path)
            .and_then(|file| file.set_modified(an_hour_ago))
            .expect("age the file");
    }
    // A path-scoped index of one unrelated file, the way `gcode index <dir>` runs it.
    let request = crate::index::api::IndexRequest {
        project_root: overlay_root,
        path_filter: Some(std::path::PathBuf::from("src/other.rs")),
        explicit_files: Vec::new(),
        full: false,
        require_cpp_semantics: false,
        sync_projections: false,
    };
    crate::index::api::index_files(
        request,
        &overlay_ctx,
        crate::index::api::IndexOptions::default(),
    )
    .expect("filtered overlay index");

    // An unscoped grep checks freshness for the whole project.
    ensure_fresh(&overlay_ctx, FreshnessScope::Project).expect("grep freshness status");
    assert_grep_finds_current_callers(&overlay_ctx);

    invalidate_test_project(&overlay_ctx);
    invalidate_test_project(&parent_ctx);
}

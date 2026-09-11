use super::super::file::{index_file, write_parsed_file_facts};
use super::super::sink::PostgresCodeFactSink;
use super::super::types::IndexTarget;
use super::super::{IndexOptions, IndexRequest, index_files, index_snapshot};
use super::fixtures::{git, git_output, write_file};
use crate::config::{CodeVectorSettings, Context, ProjectIndexScope};
use crate::db;
use crate::index::semantic::{SemanticCallRequest, SemanticCallResolver, SemanticCallTarget};
use crate::index::{api, hasher, parser};
use crate::models::{IndexedFile, IndexedProject, ParseResult, Symbol};
use crate::visibility;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

struct RewriteDuringParse {
    path: PathBuf,
    replacement: Vec<u8>,
    rewrites: usize,
}

impl SemanticCallResolver for RewriteDuringParse {
    fn resolve(
        &mut self,
        _request: &SemanticCallRequest<'_>,
    ) -> anyhow::Result<Option<SemanticCallTarget>> {
        std::fs::write(&self.path, &self.replacement)?;
        self.rewrites += 1;
        Ok(None)
    }
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn overlay_incremental_index_shadows_parent_rows_for_commit_diverged_files() {
    let (mut conn, database_url) = connect_summary_preservation_test_db();
    let base = tempfile::tempdir().expect("create temp base");
    let hooks = base.path().join("hooks");
    std::fs::create_dir_all(&hooks).expect("create hooks dir");
    let parent_root = base.path().join("parent");
    std::fs::create_dir_all(&parent_root).expect("create parent root");
    let overlay_root = base.path().join("overlay");
    let rel = "src/lib.rs";
    let parent_source = b"pub fn parent_only() {}\n";
    let overlay_source = b"pub fn overlay_only() {}\n";

    write_file(&parent_root, rel, parent_source);
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
    // The divergence lives only in a commit on the overlay branch: both
    // working trees are clean, so `git status` alone cannot see it.
    write_file(&overlay_root, rel, overlay_source);
    git(
        &overlay_root,
        &hooks,
        &["commit", "-q", "-am", "overlay change"],
    );

    let overlay_project_id = unique_test_uuid("gcode-overlay-shadow");
    let parent_project_id = unique_test_uuid("gcode-overlay-shadow-parent");
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

    // The parent is indexed on this machine with its own version of the path.
    let parent_hash = crate::index::hasher::file_content_hash(&parent_root.join(rel))
        .expect("hash parent source");
    write_postgres_parsed_file_facts_with_root(
        &mut conn,
        &parent_project_id,
        &parent_root,
        rel,
        &parent_hash,
        parent_source,
        vec![test_symbol(
            &parent_project_id,
            rel,
            &parent_hash,
            "parent_only",
            7,
            "parent-only-hash",
        )],
    );

    let ctx = Context {
        database_url,
        project_root: overlay_root.clone(),
        project_id: overlay_project_id.clone(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
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
    let visible_names = |conn: &mut postgres::Client| -> Vec<String> {
        visibility::visible_symbols_for_file(conn, &ctx, rel)
            .expect("read visible symbols")
            .into_iter()
            .map(|symbol| format!("{}:{}", symbol.project_id, symbol.name))
            .collect()
    };
    assert_eq!(
        visible_names(&mut conn),
        vec![format!("{parent_project_id}:parent_only")],
        "before reconciliation the overlay inherits the parent's rows"
    );

    let outcome = index_files(
        IndexRequest {
            project_root: overlay_root.clone(),
            path_filter: None,
            explicit_files: Vec::new(),
            full: false,
            require_cpp_semantics: false,
            sync_projections: false,
        },
        &ctx,
        IndexOptions::default(),
    )
    .expect("incremental overlay run");

    assert_eq!(
        outcome.indexed_files, 1,
        "the commit-diverged path must be indexed from the overlay tree"
    );
    assert_eq!(
        visible_names(&mut conn),
        vec![format!("{overlay_project_id}:overlay_only")],
        "overlay reads must serve the overlay's own rows for a diverged path"
    );

    // The diverged path is now shadowed by an overlay row carrying its current
    // content. A second incremental run (read-path refreshes never sync
    // projections, so adoption cannot apply) must leave it alone instead of
    // re-parsing it on every read.
    let outcome = index_files(
        IndexRequest {
            project_root: overlay_root.clone(),
            path_filter: None,
            explicit_files: Vec::new(),
            full: false,
            require_cpp_semantics: false,
            sync_projections: false,
        },
        &ctx,
        IndexOptions::default(),
    )
    .expect("second incremental overlay run");
    assert_eq!(
        outcome.indexed_files, 0,
        "a current overlay row is a finished shadow and is not re-parsed"
    );
    assert_eq!(
        visible_names(&mut conn),
        vec![format!("{overlay_project_id}:overlay_only")]
    );
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn parsed_reindex_preserves_summaries_for_immutable_content_versions() {
    let (mut conn, database_url) = connect_summary_preservation_test_db();
    let project_id = unique_test_uuid("gcode-summary-preservation");
    let rel = "src/lib.rs";
    cleanup_summary_preservation_project(&mut conn, &project_id)
        .expect("pre-clean summary preservation rows");
    let _cleanup = SummaryPreservationCleanup {
        database_url,
        project_id: project_id.clone(),
    };

    let machine_id = gobby_core::machine::read_local_machine_id().expect("read machine id");
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &IndexedProject {
            id: project_id.clone(),
            root_path: "/tmp/gcode-summary-preservation".to_string(),
            total_files: 1,
            total_symbols: 3,
            last_indexed_at: String::new(),
            index_duration_ms: 0,
            total_eligible_files: None,
            indexer_version: None,
        },
        api::IndexWriteMode::Overlay,
        true,
    )
    .expect("seed project row");

    let unchanged = test_symbol(
        &project_id,
        rel,
        "file-hash-v1",
        "unchanged",
        0,
        "unchanged-hash",
    );
    let changed = test_symbol(
        &project_id,
        rel,
        "file-hash-v1",
        "changed",
        32,
        "changed-hash-v1",
    );
    let stale = test_symbol(&project_id, rel, "file-hash-v1", "stale", 64, "stale-hash");
    write_postgres_parsed_file_facts(
        &mut conn,
        &project_id,
        rel,
        "file-hash-v1",
        b"fn unchanged() {}\nfn changed() {}\nfn stale() {}\n",
        vec![unchanged.clone(), changed.clone(), stale.clone()],
    );

    let unchanged_summary = "keep daemon summary";
    let changed_summary = "clear stale daemon summary";
    conn.execute(
        "UPDATE code_symbols SET summary = $1 WHERE id = $2",
        &[&unchanged_summary, &test_uuid_param(&unchanged.id)],
    )
    .expect("set unchanged summary");
    conn.execute(
        "UPDATE code_symbols SET summary = $1 WHERE id = $2",
        &[&changed_summary, &test_uuid_param(&changed.id)],
    )
    .expect("set changed summary");

    let mut changed_v2 = changed.clone();
    changed_v2.file_content_hash = "file-hash-v2".to_string();
    changed_v2.id = Symbol::make_id(&project_id, rel, "file-hash-v2", "changed", "function", 32);
    changed_v2.content_hash = "changed-hash-v2".to_string();
    write_postgres_parsed_file_facts(
        &mut conn,
        &project_id,
        rel,
        "file-hash-v2",
        b"// unrelated file edit\nfn unchanged() {}\nfn changed() {}\n",
        vec![unchanged.clone(), changed_v2.clone()],
    );

    assert_eq!(
        symbol_summary(&mut conn, &unchanged.id),
        Some(unchanged_summary.to_string())
    );
    assert_eq!(
        symbol_summary(&mut conn, &changed.id),
        Some(changed_summary.to_string())
    );
    assert_eq!(symbol_summary(&mut conn, &changed_v2.id), None);
    assert_eq!(
        symbol_count(&mut conn, &project_id, rel, &stale.id),
        1,
        "the prior content version remains available for other machines"
    );
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn postgres_sink_seeds_project_row_before_file_facts() {
    let (mut conn, database_url) = connect_summary_preservation_test_db();
    let project_id = unique_test_uuid("gcode-project-seed");
    let rel = "src/lib.rs";
    let root_path = Path::new("/tmp/gcode-project-seed");
    cleanup_summary_preservation_project(&mut conn, &project_id)
        .expect("pre-clean project seed rows");
    let _cleanup = SummaryPreservationCleanup {
        database_url,
        project_id: project_id.clone(),
    };
    let seeded_symbol = test_symbol(&project_id, rel, "hash-1", "seeded", 0, "hash-1");
    let seeded_symbol_id = seeded_symbol.id.clone();

    write_postgres_parsed_file_facts_with_root(
        &mut conn,
        &project_id,
        root_path,
        rel,
        "hash-1",
        b"pub fn seeded() {}\n",
        vec![seeded_symbol],
    );

    let root_path_from_db: String = conn
        .query_one(
            "SELECT root_path FROM code_indexed_project_states
             WHERE machine_id = $1 AND project_id = $2",
            &[
                &test_uuid_param(
                    &gobby_core::machine::read_local_machine_id().expect("read machine id"),
                ),
                &test_uuid_param(&project_id),
            ],
        )
        .expect("select seeded project row")
        .get(0);

    assert_eq!(root_path_from_db, root_path.to_string_lossy());
    assert_eq!(
        symbol_count(&mut conn, &project_id, rel, &seeded_symbol_id),
        1
    );
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn indexing_adopts_existing_content_version_without_reparse() {
    let (mut conn, database_url) = connect_summary_preservation_test_db();
    let project_root = tempfile::tempdir().expect("create project root");
    let project_id = unique_test_uuid("gcode-content-adoption");
    let first_machine_id = unique_test_uuid("gcode-content-adoption-first-machine");
    crate::test_env::seed_test_machine(&mut conn, &first_machine_id).expect("seed first machine");
    let rel = "src/lib.rs";
    let absolute_path = project_root.path().join(rel);
    std::fs::create_dir_all(absolute_path.parent().expect("file parent"))
        .expect("create source directory");
    std::fs::write(&absolute_path, b"pub fn adopted() {}\n").expect("write source file");
    let content_hash =
        crate::index::hasher::file_content_hash(&absolute_path).expect("hash source file");

    cleanup_summary_preservation_project(&mut conn, &project_id).expect("pre-clean adoption rows");
    let _cleanup = SummaryPreservationCleanup {
        database_url: database_url.clone(),
        project_id: project_id.clone(),
    };
    seed_primary_checkout(&mut conn, &project_id, project_root.path())
        .expect("seed primary checkout");
    api::upsert_project_stats(
        &mut conn,
        &first_machine_id,
        &IndexedProject {
            id: project_id.clone(),
            root_path: "/first-machine/repo".to_string(),
            total_files: 1,
            total_symbols: 41,
            last_indexed_at: String::new(),
            index_duration_ms: 0,
            total_eligible_files: None,
            indexer_version: None,
        },
        api::IndexWriteMode::Overlay,
        true,
    )
    .expect("seed first machine project state");
    let shared_file = IndexedFile {
        id: IndexedFile::make_id(&project_id, rel, &content_hash),
        project_id: project_id.clone(),
        file_path: rel.to_string(),
        language: "rust".to_string(),
        content_hash: content_hash.clone(),
        symbol_count: 41,
        byte_size: 4096,
        indexed_at: String::new(),
    };
    api::upsert_file(&mut conn, &shared_file).expect("seed shared content version");
    conn.execute(
        "UPDATE code_indexed_files
         SET graph_synced = TRUE, vectors_synced = TRUE
         WHERE id = $1",
        &[&test_uuid_param(&shared_file.id)],
    )
    .expect("mark shared projections complete");
    api::upsert_file_state(
        &mut conn,
        &first_machine_id,
        &shared_file,
        Path::new("/first-machine/repo"),
        api::IndexWriteMode::Overlay,
    )
    .expect("seed first machine selector");

    let ctx = Context {
        database_url,
        project_root: project_root.path().to_path_buf(),
        project_id: project_id.clone(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: ProjectIndexScope::Single,
    };
    let outcome = index_files(
        IndexRequest {
            project_root: project_root.path().to_path_buf(),
            path_filter: None,
            explicit_files: vec![absolute_path],
            full: false,
            require_cpp_semantics: false,
            sync_projections: true,
        },
        &ctx,
        IndexOptions::default(),
    )
    .expect("adopt shared content version");

    assert_eq!(outcome.skipped_files, 1);
    assert_eq!(outcome.indexed_files, 0);
    assert_eq!(outcome.symbols_indexed, 0);
    assert!(outcome.indexed_file_paths.is_empty());
    let project_uuid = test_uuid_param(&project_id);
    let shared_row = conn
        .query_one(
            "SELECT symbol_count, byte_size FROM code_indexed_files
             WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
            &[&project_uuid, &rel, &content_hash],
        )
        .expect("load adopted shared content version");
    assert_eq!(shared_row.get::<_, i32>(0), 41);
    assert_eq!(shared_row.get::<_, i32>(1), 4096);
    let selector_count: i64 = conn
        .query_one(
            "SELECT COUNT(*)::BIGINT FROM code_indexed_file_states
             WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
            &[&project_uuid, &rel, &content_hash],
        )
        .expect("count machine selectors")
        .get(0);
    assert_eq!(selector_count, 2);
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn full_indexing_reparses_previously_adopted_content() {
    let (mut conn, database_url) = connect_summary_preservation_test_db();
    let project_root = tempfile::tempdir().expect("create project root");
    let project_id = unique_test_uuid("gcode-full-reparse");
    let first_machine_id = unique_test_uuid("gcode-full-reparse-first-machine");
    crate::test_env::seed_test_machine(&mut conn, &first_machine_id).expect("seed first machine");
    let rel = "src/lib.rs";
    let content = b"pub fn adopted() {}\n";
    let absolute_path = project_root.path().join(rel);
    std::fs::create_dir_all(absolute_path.parent().expect("file parent"))
        .expect("create source directory");
    std::fs::write(&absolute_path, content).expect("write source file");
    let content_hash =
        crate::index::hasher::file_content_hash(&absolute_path).expect("hash source file");

    cleanup_summary_preservation_project(&mut conn, &project_id).expect("pre-clean reparse rows");
    let _cleanup = SummaryPreservationCleanup {
        database_url: database_url.clone(),
        project_id: project_id.clone(),
    };
    seed_primary_checkout(&mut conn, &project_id, project_root.path())
        .expect("seed primary checkout");
    api::upsert_project_stats(
        &mut conn,
        &first_machine_id,
        &IndexedProject {
            id: project_id.clone(),
            root_path: "/first-machine/repo".to_string(),
            total_files: 1,
            total_symbols: 41,
            last_indexed_at: String::new(),
            index_duration_ms: 0,
            total_eligible_files: None,
            indexer_version: None,
        },
        api::IndexWriteMode::Overlay,
        true,
    )
    .expect("seed first machine project state");
    // Seed the shared content row with wrong stats: only a real re-parse
    // corrects them, adoption trusts them as-is.
    let shared_file = IndexedFile {
        id: IndexedFile::make_id(&project_id, rel, &content_hash),
        project_id: project_id.clone(),
        file_path: rel.to_string(),
        language: "rust".to_string(),
        content_hash: content_hash.clone(),
        symbol_count: 41,
        byte_size: 4096,
        indexed_at: String::new(),
    };
    api::upsert_file(&mut conn, &shared_file).expect("seed shared content version");
    api::upsert_file_state(
        &mut conn,
        &first_machine_id,
        &shared_file,
        Path::new("/first-machine/repo"),
        api::IndexWriteMode::Overlay,
    )
    .expect("seed first machine selector");

    let ctx = Context {
        database_url,
        project_root: project_root.path().to_path_buf(),
        project_id: project_id.clone(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: ProjectIndexScope::Single,
    };
    let outcome = index_files(
        IndexRequest {
            project_root: project_root.path().to_path_buf(),
            path_filter: None,
            explicit_files: vec![absolute_path],
            full: true,
            require_cpp_semantics: false,
            sync_projections: true,
        },
        &ctx,
        IndexOptions::default(),
    )
    .expect("full reindex of adopted content");

    assert_eq!(outcome.skipped_files, 0, "full indexing must not adopt");
    assert_eq!(outcome.indexed_files, 1);
    assert_eq!(outcome.symbols_indexed, 1);
    let project_uuid = test_uuid_param(&project_id);
    let shared_row = conn
        .query_one(
            "SELECT symbol_count, byte_size FROM code_indexed_files
             WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
            &[&project_uuid, &rel, &content_hash],
        )
        .expect("load reparsed shared content version");
    assert_eq!(shared_row.get::<_, i32>(0), 1, "re-parse corrects stats");
    assert_eq!(shared_row.get::<_, i32>(1), content.len() as i32);
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn overlay_indexing_adopts_existing_content_version_without_reparse() {
    let (mut conn, database_url) = connect_summary_preservation_test_db();
    let overlay_root = tempfile::tempdir().expect("create overlay root");
    let overlay_project_id = unique_test_uuid("gcode-overlay-adoption");
    let parent_project_id = unique_test_uuid("gcode-overlay-adoption-parent");
    let first_machine_id = unique_test_uuid("gcode-overlay-adoption-first-machine");
    crate::test_env::seed_test_machine(&mut conn, &first_machine_id).expect("seed first machine");
    let rel = "src/lib.rs";
    let absolute_path = overlay_root.path().join(rel);
    std::fs::create_dir_all(absolute_path.parent().expect("file parent"))
        .expect("create source directory");
    std::fs::write(&absolute_path, b"pub fn overlay_adopted() {}\n").expect("write source file");
    let content_hash =
        crate::index::hasher::file_content_hash(&absolute_path).expect("hash source file");

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

    let machine_id = gobby_core::machine::read_local_machine_id().expect("read machine id");
    // The parent must look indexed on this machine, with a diverging version
    // of the same path so reconciliation routes the overlay file to Index.
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &IndexedProject {
            id: parent_project_id.clone(),
            root_path: "/tmp/gcode-overlay-adoption-parent".to_string(),
            total_files: 1,
            total_symbols: 1,
            last_indexed_at: String::new(),
            index_duration_ms: 0,
            total_eligible_files: None,
            indexer_version: None,
        },
        api::IndexWriteMode::Overlay,
        true,
    )
    .expect("seed parent project state");
    let parent_file = IndexedFile {
        id: IndexedFile::make_id(&parent_project_id, rel, "parent-old-hash"),
        project_id: parent_project_id.clone(),
        file_path: rel.to_string(),
        language: "rust".to_string(),
        content_hash: "parent-old-hash".to_string(),
        symbol_count: 1,
        byte_size: 64,
        indexed_at: String::new(),
    };
    api::upsert_file(&mut conn, &parent_file).expect("seed parent content version");
    api::upsert_file_state(
        &mut conn,
        &machine_id,
        &parent_file,
        Path::new("/tmp/gcode-overlay-adoption-parent"),
        api::IndexWriteMode::Overlay,
    )
    .expect("seed parent selector");

    // Another machine already parsed this exact overlay content version.
    api::upsert_project_stats(
        &mut conn,
        &first_machine_id,
        &IndexedProject {
            id: overlay_project_id.clone(),
            root_path: "/first-machine/overlay".to_string(),
            total_files: 1,
            total_symbols: 41,
            last_indexed_at: String::new(),
            index_duration_ms: 0,
            total_eligible_files: None,
            indexer_version: None,
        },
        api::IndexWriteMode::Overlay,
        true,
    )
    .expect("seed first machine overlay project state");
    let overlay_file = IndexedFile {
        id: IndexedFile::make_id(&overlay_project_id, rel, &content_hash),
        project_id: overlay_project_id.clone(),
        file_path: rel.to_string(),
        language: "rust".to_string(),
        content_hash: content_hash.clone(),
        symbol_count: 41,
        byte_size: 4096,
        indexed_at: String::new(),
    };
    api::upsert_file(&mut conn, &overlay_file).expect("seed overlay content version");
    conn.execute(
        "UPDATE code_indexed_files
         SET graph_synced = TRUE, vectors_synced = TRUE
         WHERE id = $1",
        &[&test_uuid_param(&overlay_file.id)],
    )
    .expect("mark overlay projections complete");
    api::upsert_file_state(
        &mut conn,
        &first_machine_id,
        &overlay_file,
        Path::new("/first-machine/overlay"),
        api::IndexWriteMode::Overlay,
    )
    .expect("seed first machine overlay selector");

    let ctx = Context {
        database_url,
        project_root: overlay_root.path().to_path_buf(),
        project_id: overlay_project_id.clone(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: ProjectIndexScope::Overlay {
            overlay_project_id: overlay_project_id.clone(),
            overlay_root: overlay_root.path().to_path_buf(),
            parent_project_id: parent_project_id.clone(),
            parent_root: std::path::PathBuf::from("/tmp/gcode-overlay-adoption-parent"),
        },
    };
    let outcome = index_files(
        IndexRequest {
            project_root: overlay_root.path().to_path_buf(),
            path_filter: None,
            explicit_files: Vec::new(),
            full: false,
            require_cpp_semantics: false,
            sync_projections: true,
        },
        &ctx,
        IndexOptions::default(),
    )
    .expect("overlay adoption run");

    assert_eq!(
        outcome.indexed_files, 0,
        "overlay run must adopt, not parse"
    );
    assert_eq!(outcome.symbols_indexed, 0);
    assert_eq!(outcome.skipped_files, 1);
    let overlay_uuid = test_uuid_param(&overlay_project_id);
    let shared_row = conn
        .query_one(
            "SELECT symbol_count FROM code_indexed_files
             WHERE project_id = $1 AND file_path = $2 AND content_hash = $3",
            &[&overlay_uuid, &rel, &content_hash],
        )
        .expect("load adopted overlay content version");
    assert_eq!(
        shared_row.get::<_, i32>(0),
        41,
        "adoption must not re-parse the seeded stats"
    );
    let machine_uuid = test_uuid_param(&machine_id);
    let adopted_hash: String = conn
        .query_one(
            "SELECT content_hash FROM code_indexed_file_states
             WHERE machine_id = $1 AND project_id = $2 AND file_path = $3",
            &[&machine_uuid, &overlay_uuid, &rel],
        )
        .expect("load adopted overlay selector")
        .get(0);
    assert_eq!(adopted_hash, content_hash);
    let overlay_stats = conn
        .query_one(
            "SELECT total_files, total_symbols FROM code_indexed_project_states
             WHERE machine_id = $1 AND project_id = $2",
            &[&machine_uuid, &overlay_uuid],
        )
        .expect("load refreshed overlay stats");
    assert_eq!(overlay_stats.get::<_, i32>(0), 1);
    assert_eq!(overlay_stats.get::<_, i32>(1), 0);
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn explicit_file_index_preserves_last_indexed_at_until_full_index() {
    let (mut conn, database_url) = connect_summary_preservation_test_db();
    let project_root = tempfile::tempdir().expect("create project root");
    let project_id = unique_test_uuid("gcode-explicit-index-stamp");
    let rel = "src/lib.rs";
    let absolute_path = project_root.path().join(rel);
    std::fs::create_dir_all(absolute_path.parent().expect("file parent"))
        .expect("create source directory");
    std::fs::write(&absolute_path, b"pub fn indexed() {}\n").expect("write source file");

    cleanup_summary_preservation_project(&mut conn, &project_id)
        .expect("pre-clean explicit index rows");
    let _cleanup = SummaryPreservationCleanup {
        database_url: database_url.clone(),
        project_id: project_id.clone(),
    };
    seed_primary_checkout(&mut conn, &project_id, project_root.path())
        .expect("seed primary checkout");

    let machine_id = gobby_core::machine::read_local_machine_id().expect("read machine id");
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &IndexedProject {
            id: project_id.clone(),
            root_path: project_root.path().to_string_lossy().to_string(),
            total_files: 0,
            total_symbols: 0,
            last_indexed_at: String::new(),
            index_duration_ms: 0,
            total_eligible_files: None,
            indexer_version: None,
        },
        api::IndexWriteMode::Primary,
        true,
    )
    .expect("seed project stats");
    let project_uuid = test_uuid_param(&project_id);
    let machine_uuid = test_uuid_param(&machine_id);
    conn.execute(
        "UPDATE code_indexed_project_states
         SET last_indexed_at = TIMESTAMPTZ '2000-01-01 00:00:00+00'
         WHERE machine_id = $1 AND project_id = $2",
        &[&machine_uuid, &project_uuid],
    )
    .expect("set baseline index timestamp");

    let ctx = Context {
        database_url,
        project_root: project_root.path().to_path_buf(),
        project_id: project_id.clone(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: ProjectIndexScope::Single,
    };
    let last_indexed_at = |conn: &mut postgres::Client| -> String {
        conn.query_one(
            "SELECT last_indexed_at::TEXT FROM code_indexed_project_states
             WHERE machine_id = $1 AND project_id = $2",
            &[&machine_uuid, &project_uuid],
        )
        .expect("load last indexed timestamp")
        .get(0)
    };
    let baseline = last_indexed_at(&mut conn);

    index_files(
        IndexRequest {
            project_root: project_root.path().to_path_buf(),
            path_filter: None,
            explicit_files: vec![absolute_path],
            full: false,
            require_cpp_semantics: false,
            sync_projections: false,
        },
        &ctx,
        IndexOptions::default(),
    )
    .expect("index explicit file");
    assert_eq!(last_indexed_at(&mut conn), baseline);

    index_files(
        IndexRequest {
            project_root: project_root.path().to_path_buf(),
            path_filter: None,
            explicit_files: Vec::new(),
            full: true,
            require_cpp_semantics: false,
            sync_projections: false,
        },
        &ctx,
        IndexOptions::default(),
    )
    .expect("index full project");
    assert_ne!(last_indexed_at(&mut conn), baseline);
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn parsed_snapshot_keeps_parent_and_symbol_content_keys_consistent_during_rewrite() {
    let (mut conn, database_url) = connect_summary_preservation_test_db();
    let project_root = tempfile::tempdir().expect("create project root");
    let project_id = unique_test_uuid("gcode-index-snapshot-race");
    let rel = "src/race.py";
    let absolute_path = project_root.path().join(rel);
    let original: &[u8] = b"def indexed_snapshot():\n    unresolved_during_parse()\n";
    let replacement: &[u8] = b"def replacement_snapshot():\n    pass\n";

    std::fs::create_dir_all(absolute_path.parent().expect("file parent"))
        .expect("create source directory");
    std::fs::write(&absolute_path, original).expect("write original source");
    cleanup_summary_preservation_project(&mut conn, &project_id)
        .expect("pre-clean snapshot race rows");
    let _cleanup = SummaryPreservationCleanup {
        database_url,
        project_id: project_id.clone(),
    };
    seed_primary_checkout(&mut conn, &project_id, project_root.path())
        .expect("seed primary checkout");

    let import_context = parser::build_import_resolution_context(
        project_root.path(),
        std::slice::from_ref(&absolute_path),
    );
    let mut resolver = RewriteDuringParse {
        path: absolute_path.clone(),
        replacement: replacement.to_vec(),
        rewrites: 0,
    };
    let excludes = Vec::<String>::new();
    let counts = index_file(
        &mut conn,
        &absolute_path,
        IndexTarget {
            project_id: &project_id,
            root_path: project_root.path(),
            mode: api::IndexWriteMode::Primary,
        },
        &excludes,
        &import_context,
        Some(&mut resolver),
    )
    .expect("index the captured source snapshot")
    .expect("source remains indexable");

    assert!(resolver.rewrites > 0, "test must rewrite during parsing");
    assert_eq!(
        std::fs::read(&absolute_path).expect("read rewritten source"),
        replacement
    );
    assert!(counts.symbols_indexed > 0);

    let project_uuid = test_uuid_param(&project_id);
    let original_hash = hasher::content_hash(original);
    let parent = conn
        .query_one(
            "SELECT content_hash, byte_size
             FROM code_indexed_files
             WHERE project_id = $1 AND file_path = $2",
            &[&project_uuid, &rel],
        )
        .expect("load indexed parent snapshot");
    assert_eq!(parent.get::<_, String>(0), original_hash);
    assert_eq!(
        parent.get::<_, i32>(1),
        i32::try_from(original.len()).expect("source size fits i32")
    );

    let symbol_hashes = conn
        .query(
            "SELECT DISTINCT file_content_hash
             FROM code_symbols
             WHERE project_id = $1 AND file_path = $2",
            &[&project_uuid, &rel],
        )
        .expect("load indexed symbol snapshots")
        .into_iter()
        .map(|row| row.get::<_, String>(0))
        .collect::<Vec<_>>();
    assert_eq!(symbol_hashes, vec![original_hash]);

    let orphan_count: i64 = conn
        .query_one(
            "SELECT COUNT(*)::BIGINT
             FROM code_symbols s
             LEFT JOIN code_indexed_files f
               ON f.project_id = s.project_id
              AND f.file_path = s.file_path
              AND f.content_hash = s.file_content_hash
             WHERE s.project_id = $1 AND f.id IS NULL",
            &[&project_uuid],
        )
        .expect("check symbol parent integrity")
        .get(0);
    assert_eq!(orphan_count, 0);
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn immutable_snapshot_skips_clangd_but_keeps_tree_sitter_facts() {
    let (mut conn, database_url) = connect_summary_preservation_test_db();
    let base = tempfile::tempdir().expect("create snapshot base");
    let hooks = base.path().join("hooks");
    std::fs::create_dir_all(&hooks).expect("create hooks directory");
    let repository = base.path().join("repository");
    std::fs::create_dir_all(&repository).expect("create repository");
    let project_root = base.path().join("project");
    write_file(
        &repository,
        "compile_commands.json",
        br#"[{"directory":".","command":"cc -c src/main.c","file":"src/main.c"}]"#,
    );
    write_file(
        &repository,
        "src/main.c",
        b"int helper(void) { return 1; }\nint main(void) { return helper(); }\n",
    );
    git(&repository, &hooks, &["init", "-q"]);
    git(&repository, &hooks, &["add", "."]);
    git(&repository, &hooks, &["commit", "-q", "-m", "snapshot"]);
    let project_root_arg = project_root.to_string_lossy();
    git(
        &repository,
        &hooks,
        &[
            "worktree",
            "add",
            "-q",
            "--detach",
            &project_root_arg,
            "HEAD",
        ],
    );
    let commit_oid = git_output(&project_root, &hooks, &["rev-parse", "HEAD"]);

    let project_id = unique_test_uuid("gcode-index-snapshot-no-clangd");
    cleanup_summary_preservation_project(&mut conn, &project_id).expect("pre-clean snapshot rows");
    let _cleanup = SummaryPreservationCleanup {
        database_url: database_url.clone(),
        project_id: project_id.clone(),
    };
    seed_primary_checkout(&mut conn, &project_id, &project_root).expect("seed snapshot checkout");
    let ctx = Context {
        database_url,
        project_root: project_root.clone(),
        project_id,
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: ProjectIndexScope::Single,
    };

    let outcome = temp_env::with_vars(
        [
            ("GCODE_REQUIRE_CPP_SEMANTICS", Some("1")),
            ("GCODE_CLANGD", Some("/definitely/missing/clangd")),
        ],
        || index_snapshot(&ctx, &commit_oid),
    )
    .expect("index immutable snapshot without launching clangd");

    assert_eq!(outcome.scanned_files, 2);
    assert!(outcome.symbols_indexed >= 2);
}

fn connect_summary_preservation_test_db() -> (postgres::Client, String) {
    let database_url = crate::test_env::postgres_test_database_url("indexer serial DB tests");
    let conn = db::connect_readwrite(&database_url)
        .expect("connect summary preservation PostgreSQL test database");
    (conn, database_url)
}

fn unique_test_uuid(prefix: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time after epoch")
        .as_nanos();
    uuid::Uuid::new_v5(
        &crate::models::CODE_INDEX_UUID_NAMESPACE,
        format!("{prefix}-{}-{nanos}", std::process::id()).as_bytes(),
    )
    .to_string()
}

fn test_uuid_param(id: &str) -> uuid::Uuid {
    db::id_param(id).expect("test id is a uuid")
}

fn seed_primary_checkout(
    conn: &mut postgres::Client,
    project_id: &str,
    root_path: &Path,
) -> anyhow::Result<()> {
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    crate::test_env::seed_test_machine(conn, &machine_id).map_err(anyhow::Error::msg)?;
    let machine_id = test_uuid_param(&machine_id);
    let project_uuid = test_uuid_param(project_id);
    conn.execute(
        "INSERT INTO projects (id, name) VALUES ($1, $2)",
        &[&project_uuid, &format!("serial-db-{project_id}")],
    )?;
    conn.execute(
        "INSERT INTO project_checkouts (machine_id, project_id, root_path)
         VALUES ($1, $2, $3)",
        &[&machine_id, &project_uuid, &root_path.to_string_lossy()],
    )?;
    Ok(())
}

struct SummaryPreservationCleanup {
    database_url: String,
    project_id: String,
}

impl Drop for SummaryPreservationCleanup {
    fn drop(&mut self) {
        if let Ok(mut conn) = db::connect_readwrite(&self.database_url) {
            let _ = cleanup_summary_preservation_project(&mut conn, &self.project_id);
        }
    }
}

fn cleanup_summary_preservation_project(
    conn: &mut postgres::Client,
    project_id: &str,
) -> anyhow::Result<()> {
    let project_id = db::id_param(project_id)?;
    let mut tx = conn.transaction()?;
    tx.execute(
        "DELETE FROM code_indexed_file_states WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_indexed_project_states WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_calls WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_inheritance WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_imports WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_content_chunks WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_symbols WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_indexed_files WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM code_indexed_projects WHERE id = $1",
        &[&project_id],
    )?;
    tx.execute(
        "DELETE FROM project_checkouts WHERE project_id = $1",
        &[&project_id],
    )?;
    tx.execute("DELETE FROM projects WHERE id = $1", &[&project_id])?;
    tx.commit()?;
    Ok(())
}

fn write_postgres_parsed_file_facts(
    conn: &mut postgres::Client,
    project_id: &str,
    rel: &str,
    file_hash: &str,
    source: &[u8],
    symbols: Vec<Symbol>,
) {
    write_postgres_parsed_file_facts_with_root(
        conn,
        project_id,
        Path::new("/tmp/gcode-summary-preservation"),
        rel,
        file_hash,
        source,
        symbols,
    );
}

fn write_postgres_parsed_file_facts_with_root(
    conn: &mut postgres::Client,
    project_id: &str,
    root_path: &Path,
    rel: &str,
    file_hash: &str,
    source: &[u8],
    symbols: Vec<Symbol>,
) {
    let parse_result = ParseResult {
        symbols,
        imports: Vec::new(),
        calls: Vec::new(),
        inheritance: Vec::new(),
        source: source.to_vec(),
    };
    let mut tx = conn.transaction().expect("start parsed write transaction");
    let mut sink =
        PostgresCodeFactSink::new(&mut tx, project_id, root_path, api::IndexWriteMode::Overlay)
            .expect("seed project row");
    write_parsed_file_facts(
        &mut sink,
        project_id,
        rel,
        "rust",
        file_hash,
        source.len(),
        &parse_result,
    )
    .expect("write parsed file facts to PostgreSQL");
    tx.commit().expect("commit parsed write transaction");
}

fn test_symbol(
    project_id: &str,
    rel: &str,
    file_content_hash: &str,
    name: &str,
    byte_start: usize,
    content_hash: &str,
) -> Symbol {
    Symbol {
        id: Symbol::make_id(
            project_id,
            rel,
            file_content_hash,
            name,
            "function",
            byte_start,
        ),
        project_id: project_id.to_string(),
        file_path: rel.to_string(),
        name: name.to_string(),
        qualified_name: name.to_string(),
        kind: "function".to_string(),
        language: "rust".to_string(),
        byte_start,
        byte_end: byte_start + name.len(),
        line_start: 1,
        line_end: 1,
        signature: Some(format!("fn {name}()")),
        docstring: None,
        parent_symbol_id: None,
        file_content_hash: file_content_hash.to_string(),
        content_hash: content_hash.to_string(),
        summary: None,
        created_at: String::new(),
        updated_at: String::new(),
    }
}

fn symbol_summary(conn: &mut postgres::Client, symbol_id: &str) -> Option<String> {
    conn.query_one(
        "SELECT summary FROM code_symbols WHERE id = $1",
        &[&test_uuid_param(symbol_id)],
    )
    .expect("query symbol summary")
    .try_get(0)
    .expect("decode symbol summary")
}

fn symbol_count(conn: &mut postgres::Client, project_id: &str, rel: &str, symbol_id: &str) -> i64 {
    conn.query_one(
        "SELECT COUNT(*)::BIGINT
         FROM code_symbols
         WHERE project_id = $1 AND file_path = $2 AND id = $3",
        &[
            &test_uuid_param(project_id),
            &rel,
            &test_uuid_param(symbol_id),
        ],
    )
    .expect("query symbol count")
    .try_get(0)
    .expect("decode symbol count")
}

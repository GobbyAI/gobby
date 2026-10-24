use std::collections::HashMap;

use super::{
    SearchOptions, TextSearchOptions, resolve_graph_symbols, resolve_hybrid_symbols, search,
    search_content, search_symbol, search_text,
};
use crate::commands::grep::db_tests::{
    fixture_uuid, insert_chunk_version, insert_file_version, insert_machine_state,
    unique_project_id,
};
use crate::{
    commands::indexed_paths,
    config::{CodeVectorSettings, Context, ProjectIndexScope},
    index::api,
    models::{CODE_INDEX_UUID_NAMESPACE, IndexedFile, IndexedProject, Symbol},
    output::Format,
    search::fts,
    visibility,
};

fn indexed_symbol(project_id: &str, file_path: &str) -> Symbol {
    Symbol {
        id: "hybrid-deleted-symbol".to_string(),
        project_id: project_id.to_string(),
        file_path: file_path.to_string(),
        name: "deleted_symbol".to_string(),
        qualified_name: "deleted_symbol".to_string(),
        kind: "function".to_string(),
        language: "rust".to_string(),
        byte_start: 0,
        byte_end: 10,
        line_start: 1,
        line_end: 1,
        signature: None,
        docstring: None,
        parent_symbol_id: None,
        file_content_hash: "indexed-before-delete".to_string(),
        content_hash: String::new(),
        summary: None,
        created_at: String::new(),
        updated_at: String::new(),
    }
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn hybrid_search_excludes_indexed_file_deleted_from_disk() -> anyhow::Result<()> {
    let project_root = tempfile::tempdir()?;
    let file_path = "src/lib.rs";
    let absolute_path = project_root.path().join(file_path);
    std::fs::create_dir_all(absolute_path.parent().expect("file has parent"))?;
    std::fs::write(&absolute_path, "fn deleted_symbol() {}\n")?;

    let database_url = crate::test_env::postgres_test_database_url("hybrid deleted file");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)?;
    let project_id = uuid::Uuid::new_v5(
        &CODE_INDEX_UUID_NAMESPACE,
        project_root.path().to_string_lossy().as_bytes(),
    )
    .to_string();
    let project = IndexedProject {
        id: project_id.clone(),
        root_path: project_root.path().to_string_lossy().into_owned(),
        total_files: 1,
        total_symbols: 1,
        last_indexed_at: String::new(),
        index_duration_ms: 0,
        total_eligible_files: Some(1),
        indexer_version: None,
    };
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &project,
        api::IndexWriteMode::Overlay,
        true,
    )?;
    let indexed_file = IndexedFile {
        id: IndexedFile::make_id(&project_id, file_path, "indexed-before-delete"),
        project_id: project_id.clone(),
        file_path: file_path.to_string(),
        language: "rust".to_string(),
        content_hash: "indexed-before-delete".to_string(),
        symbol_count: 1,
        byte_size: 23,
        indexed_at: String::new(),
    };
    api::upsert_file(&mut conn, &indexed_file)?;
    api::file_state::upsert_file_state(
        &mut conn,
        &machine_id,
        &indexed_file,
        project_root.path(),
        api::IndexWriteMode::Overlay,
    )?;

    let ctx = Context {
        database_url,
        project_root: project_root.path().to_path_buf(),
        project_id: project_id.clone(),
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
        index_scope: ProjectIndexScope::Single,
    };
    let indexed_symbol = indexed_symbol(&project_id, file_path);
    let merged = vec![(indexed_symbol.id.clone(), 1.0, vec!["fts".to_string()])];
    let symbol_cache = HashMap::from([(indexed_symbol.id.clone(), indexed_symbol)]);

    std::fs::remove_file(absolute_path)?;
    let resolved = resolve_hybrid_symbols(&mut conn, &ctx, &merged, &symbol_cache, None, None, &[]);

    // Selector rows restrict content-version deletes, so drop them before the
    // project row cascades through code_indexed_files.
    let project_uuid = crate::db::id_param(&project_id)?;
    conn.execute(
        "DELETE FROM code_indexed_file_states WHERE project_id = $1",
        &[&project_uuid],
    )?;
    conn.execute(
        "DELETE FROM code_indexed_project_states WHERE project_id = $1",
        &[&project_uuid],
    )?;
    conn.execute(
        "DELETE FROM code_indexed_projects WHERE id = $1",
        &[&project_uuid],
    )?;

    assert!(resolved.is_empty());
    Ok(())
}

const BATCH_FILE_COUNT: usize = 12;
const BATCH_MARKER: &str = "searchbatchmarker";

fn insert_symbol_version(
    conn: &mut postgres::Client,
    project_id: &uuid::Uuid,
    file_path: &str,
    name: &str,
    content_hash: &str,
) {
    let id = fixture_uuid(&format!("{project_id}:{file_path}:{name}:{content_hash}"));
    conn.execute(
        "INSERT INTO code_symbols
                (id, project_id, file_path, name, qualified_name, kind, language, byte_start,
                 byte_end, line_start, line_end, signature, docstring, parent_symbol_id,
                 file_content_hash, content_hash, summary, created_at, updated_at)
             VALUES ($1, $2, $3, $4, $4, 'function', 'rust', 0, 1, 1, 1, $4, NULL, NULL,
                     $5, $5, $4, NOW(), NOW())",
        &[&id, project_id, &file_path, &name, &content_hash],
    )
    .expect("insert symbol version");
}

struct SearchBatchCleanup {
    database_url: String,
    project_id: uuid::Uuid,
}

impl Drop for SearchBatchCleanup {
    fn drop(&mut self) {
        if let Ok(mut conn) = gobby_core::postgres::connect_readwrite(&self.database_url) {
            for sql in [
                "DELETE FROM code_symbols WHERE project_id = $1",
                "DELETE FROM code_indexed_file_states WHERE project_id = $1",
                "DELETE FROM code_indexed_project_states WHERE project_id = $1",
                "DELETE FROM code_content_chunks WHERE project_id = $1",
                "DELETE FROM code_indexed_files WHERE project_id = $1",
                "DELETE FROM code_indexed_projects WHERE id = $1",
            ] {
                let _ = conn.execute(sql, &[&self.project_id]);
            }
        }
    }
}

struct SearchBatchFixture {
    _cleanup: SearchBatchCleanup,
    conn: postgres::Client,
    ctx: Context,
    _root: tempfile::TempDir,
}

fn seed_search_batch(project_tag: &str) -> SearchBatchFixture {
    let database_url = crate::test_env::postgres_test_database_url("search PostgreSQL tests");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)
        .expect("connect search PostgreSQL test database");
    crate::schema::validate_runtime_schema(&mut conn).expect("search test schema is valid");

    let root = tempfile::tempdir().expect("create temp project root");
    std::fs::create_dir_all(root.path().join("src")).expect("create src dir");
    let file_paths: Vec<String> = (0..BATCH_FILE_COUNT)
        .map(|index| format!("src/file_{index:02}.rs"))
        .collect();
    for file_path in &file_paths {
        std::fs::write(root.path().join(file_path), format!("{BATCH_MARKER}\n"))
            .expect("write fixture file");
    }

    let project_id = unique_project_id(project_tag);
    let project_uuid = crate::db::id_param(&project_id).expect("project uuid");
    let cleanup = SearchBatchCleanup {
        database_url: database_url.clone(),
        project_id: project_uuid,
    };
    conn.execute(
        "INSERT INTO code_indexed_projects (id) VALUES ($1)",
        &[&project_uuid],
    )
    .expect("insert project");

    let local_machine = crate::db::id_param(
        &gobby_core::machine::read_local_machine_id().expect("read local machine id"),
    )
    .expect("local machine uuid");
    let root_path = root.path().to_string_lossy().to_string();
    for (index, file_path) in file_paths.iter().enumerate() {
        let content_hash = format!("hash-{index:02}");
        insert_file_version(&mut conn, &project_uuid, file_path, &content_hash);
        insert_machine_state(
            &mut conn,
            &local_machine,
            &project_uuid,
            &root_path,
            file_path,
            &content_hash,
        );
        insert_chunk_version(
            &mut conn,
            &project_uuid,
            file_path,
            BATCH_MARKER,
            &content_hash,
        );
        insert_symbol_version(
            &mut conn,
            &project_uuid,
            file_path,
            BATCH_MARKER,
            &content_hash,
        );
    }

    let ctx = Context {
        database_url: database_url.clone(),
        project_root: root.path().to_path_buf(),
        project_id: project_id.clone(),
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
        index_scope: ProjectIndexScope::Single,
    };
    SearchBatchFixture {
        _cleanup: cleanup,
        conn,
        ctx,
        _root: root,
    }
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn search_checks_indexed_paths_in_one_batch() {
    let SearchBatchFixture {
        _cleanup,
        mut conn,
        ctx,
        _root,
    } = seed_search_batch("search-one-batch");
    let options = || TextSearchOptions {
        limit: 100,
        offset: 0,
        language: None,
        paths: &[],
        format: Format::Json,
        token_budget: None,
        verbose: false,
    };

    let symbol_hits = fts::search_text_visible(&mut conn, BATCH_MARKER, &ctx, None, &[], 100)
        .expect("query symbol fixtures");
    assert_eq!(
        symbol_hits.len(),
        BATCH_FILE_COUNT,
        "fixture must yield one symbol hit per file"
    );
    let content_hits = fts::search_content_visible(&mut conn, BATCH_MARKER, &ctx, None, &[], 100)
        .expect("query content fixtures");
    assert_eq!(
        content_hits.len(),
        BATCH_FILE_COUNT,
        "fixture must yield one content hit per file"
    );

    indexed_paths::reset_lookup_count();
    search_text(&ctx, BATCH_MARKER, options()).expect("search text");
    assert_eq!(
        indexed_paths::lookup_count(),
        1,
        "search text must check indexed paths with one set-based query, not one per result"
    );

    indexed_paths::reset_lookup_count();
    search_content(&ctx, BATCH_MARKER, options()).expect("search content");
    assert_eq!(
        indexed_paths::lookup_count(),
        1,
        "search content must check indexed paths with one set-based query, not one per result"
    );
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn symbol_search_checks_indexed_paths_in_one_batch() {
    let SearchBatchFixture {
        _cleanup,
        mut conn,
        ctx,
        _root,
    } = seed_search_batch("symbol-search-one-batch");
    let exact_hits = fts::search_symbols_exact_first_visible(
        &mut conn,
        BATCH_MARKER,
        &ctx,
        None,
        None,
        &[],
        100,
    )
    .expect("query symbol fixtures");
    assert_eq!(
        exact_hits.len(),
        BATCH_FILE_COUNT,
        "fixture must yield one exact symbol hit per file"
    );

    let search_options = |with_graph: bool| SearchOptions {
        limit: 100,
        offset: 0,
        kind: None,
        language: None,
        paths: &[],
        format: Format::Json,
        with_graph,
        token_budget: None,
        verbose: false,
    };
    for with_graph in [false, true] {
        indexed_paths::reset_lookup_count();
        search(&ctx, BATCH_MARKER, search_options(with_graph)).expect("search");
        assert_eq!(
            indexed_paths::lookup_count(),
            1,
            "search (with_graph={with_graph}) must check indexed paths with one set-based query, not one per symbol"
        );

        indexed_paths::reset_lookup_count();
        search_symbol(&ctx, BATCH_MARKER, search_options(with_graph)).expect("search symbol");
        assert_eq!(
            indexed_paths::lookup_count(),
            1,
            "search symbol (with_graph={with_graph}) must check indexed paths with one set-based query, not one per symbol"
        );
    }
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn graph_symbol_misses_resolve_in_one_visibility_batch() {
    let SearchBatchFixture {
        _cleanup,
        mut conn,
        ctx,
        _root,
    } = seed_search_batch("graph-symbol-miss-one-batch");
    let symbols = fts::search_symbols_exact_first_visible(
        &mut conn,
        BATCH_MARKER,
        &ctx,
        None,
        None,
        &[],
        100,
    )
    .expect("query symbol fixtures");
    assert_eq!(
        symbols.len(),
        BATCH_FILE_COUNT,
        "fixture must yield one visible symbol per file"
    );
    let merged: Vec<(String, f64, Vec<String>)> = symbols
        .iter()
        .map(|sym| (sym.id.clone(), 1.0, vec!["graph".to_string()]))
        .collect();

    visibility::reset_symbol_lookup_counts();
    let resolved = resolve_graph_symbols(&mut conn, &ctx, &merged, HashMap::new(), None, None, &[])
        .expect("resolve graph symbols");

    assert_eq!(
        visibility::per_id_symbol_lookup_count(),
        0,
        "cache-miss symbols must resolve with one batched visibility query, not one per symbol"
    );
    assert_eq!(
        visibility::batch_symbol_lookup_count(),
        1,
        "cache-miss symbols must resolve with exactly one batched visibility query"
    );
    let resolved_ids: Vec<&str> = resolved.iter().map(|(sym, _, _)| sym.id.as_str()).collect();
    let merged_ids: Vec<&str> = merged.iter().map(|(id, _, _)| id.as_str()).collect();
    assert_eq!(
        resolved_ids, merged_ids,
        "resolved symbols must keep merged order"
    );
}

use std::collections::HashMap;

use super::resolve_hybrid_symbols;
use crate::{
    config::{CodeVectorSettings, Context, ProjectIndexScope},
    index::api,
    models::{CODE_INDEX_UUID_NAMESPACE, IndexedFile, IndexedProject, Symbol},
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
    api::upsert_file_state(
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

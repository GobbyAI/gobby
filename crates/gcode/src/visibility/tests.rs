use super::*;
use std::path::PathBuf;

#[test]
fn visible_project_ids_include_overlay_before_parent() {
    let ctx = Context {
        database_url: String::new(),
        project_root: PathBuf::from("/worktree"),
        project_id: "overlay".to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: ProjectIndexScope::Overlay {
            overlay_project_id: "overlay".to_string(),
            overlay_root: PathBuf::from("/worktree"),
            parent_project_id: "parent".to_string(),
            parent_root: PathBuf::from("/parent"),
        },
    };

    assert_eq!(visible_project_ids(&ctx), vec!["overlay", "parent"]);
}

#[test]
fn symbols_for_file_sql_qualifies_joined_symbol_columns() {
    let sql = symbols_for_files_sql();

    assert!(sql.contains("SELECT cs.id, cs.project_id, cs.file_path"));
    assert!(sql.contains("FROM code_symbols cs"));
    assert!(sql.contains("JOIN code_indexed_files cf"));
    assert!(sql.contains("cs.file_path = ANY($3)"));
    assert!(!sql.contains("SELECT id, project_id, file_path"));
}

#[test]
fn overlay_symbols_for_files_sql_batches_paths_and_preserves_overlay_shadowing() {
    let sql = overlay_symbols_for_files_sql();

    assert!(sql.contains("SELECT cs.id, cs.project_id, cs.file_path"));
    assert!(sql.contains("cs.file_path = ANY($4)"));
    assert!(sql.contains("cs.project_id = $2"));
    assert!(sql.contains("cs.project_id = $3"));
    assert!(sql.contains("NOT EXISTS"));
    assert!(sql.contains("shadow.project_id = $2"));
    assert!(sql.contains("shadow.file_path = cs.file_path"));
    assert!(!sql.contains("cs.file_path = $4"));
}

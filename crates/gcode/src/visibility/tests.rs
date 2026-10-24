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
        machine_id: gobby_core::machine::read_local_machine_id().expect("read machine id"),
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
    let sql = symbols_for_files_sql(false);

    assert!(sql.contains("SELECT cs.id, cs.project_id, cs.file_path"));
    assert!(sql.contains("FROM code_symbols cs"));
    assert!(sql.contains("JOIN code_indexed_files cf"));
    assert!(sql.contains("cs.file_path = ANY($3)"));
    assert!(!sql.contains("SELECT id, project_id, file_path"));
}

#[test]
fn overlay_symbols_for_files_sql_batches_paths_and_preserves_overlay_shadowing() {
    let sql = symbols_for_files_sql(true);

    assert!(sql.contains("SELECT cs.id, cs.project_id, cs.file_path"));
    assert!(sql.contains("cs.file_path = ANY($3)"));
    assert!(sql.contains("fs.project_id = $2"));
    assert!(sql.contains("FROM code_overlay_pins p"));
    assert!(sql.contains("NOT EXISTS"));
    assert!(sql.contains("shadow.project_id = $2"));
    assert!(sql.contains("shadow.file_path = p.file_path"));
    assert!(!sql.contains("cs.file_path = $4"));
}
#[path = "pin_fixture.rs"]
mod pin_fixture;

#[test]
#[cfg_attr(not(gcode_postgres_tests), ignore = "requires PostgreSQL")]
#[serial_test::serial(serial_db)]
fn pinned_reads_survive_parent_reindex() -> anyhow::Result<()> {
    use crate::codewiki_facts::{CodewikiFacts, FileId, ScopeSelector};
    use crate::search::fts;
    let mut fixture = pin_fixture::PinFixture::new()?;
    let parent = fixture.parent();
    let old = fixture.version(&parent, "kept.rs", "old", "pinned_original")?;
    fixture.pin(&parent, "kept.rs", "old")?;
    let moved = fixture.version(&parent, "kept.rs", "new", "moving_parent")?;
    fixture.version(&parent, "unselected.rs", "new", "unpinned_parent")?;
    let ctx = fixture.ctx.clone();
    let tree = visible_tree(&mut fixture.conn, &ctx)?;
    assert_eq!(
        tree.iter()
            .map(|f| (f.file_path.as_str(), f.content_hash.as_str()))
            .collect::<Vec<_>>(),
        vec![("kept.rs", "old")]
    );
    assert!(symbol_is_visible(&mut fixture.conn, &ctx, &old));
    assert!(!symbol_is_visible(&mut fixture.conn, &ctx, &moved));
    assert_eq!(
        visible_symbols_for_file(&mut fixture.conn, &ctx, "kept.rs")?[0].id,
        old.id
    );
    assert_eq!(
        visible_symbols_by_ids(&mut fixture.conn, &ctx, &[old.id.clone(), moved.id])?[0].id,
        old.id
    );
    assert_eq!(
        fts::search_symbols_fts_visible(
            &mut fixture.conn,
            "scopewindowneedle",
            &ctx,
            None,
            None,
            &[],
            10
        )?[0]
            .id,
        old.id
    );
    let content =
        fts::search_content_visible(&mut fixture.conn, "scopewindowneedle", &ctx, None, &[], 10)?;
    assert_eq!(content.len(), 1);
    assert!(content[0].snippet.contains("pinned_original"));
    let imports = crate::communities::identity::load_project_imports(&mut fixture.conn, &ctx)?;
    assert_eq!(
        imports.rows,
        vec![("kept.rs".into(), "pinned_original".into())]
    );
    let facts = CodewikiFacts::from_context(ctx);
    assert_eq!(
        facts.scoped_files(&ScopeSelector::all())?[0].content_hash,
        "old"
    );
    assert_eq!(
        facts.symbols_for_file(&FileId::new("kept.rs"))?[0].id,
        old.id
    );
    assert!(
        facts.leading_chunks(&[FileId::new("kept.rs")])?[0]
            .content
            .contains("pinned_original")
    );
    let grep = facts.grep("pinned_original", &ScopeSelector::all())?;
    assert_eq!(grep.hits.len(), 1);
    assert!(grep.hits[0].text.contains("pinned_original"));
    Ok(())
}

#[test]
#[cfg_attr(not(gcode_postgres_tests), ignore = "requires PostgreSQL")]
#[serial_test::serial(serial_db)]
fn overlay_shadowing_of_pins() -> anyhow::Result<()> {
    let mut fixture = pin_fixture::PinFixture::new()?;
    let source = fixture.source.clone();
    let overlay = fixture.ctx.project_id.clone();
    for path in ["inherited.rs", "shadowed.rs", "deleted.rs"] {
        fixture.version(&source, path, "base", "ancestor")?;
        fixture.pin(&source, path, "base")?;
    }
    let owned = fixture.version(&overlay, "shadowed.rs", "child", "child_symbol")?;
    fixture.version(&overlay, "deleted.rs", TOMBSTONE_HASH, "deleted")?;
    let ctx = fixture.ctx.clone();
    let tree = visible_tree(&mut fixture.conn, &ctx)?;
    assert_eq!(
        tree.iter()
            .map(|f| (f.file_path.as_str(), f.content_hash.as_str()))
            .collect::<Vec<_>>(),
        vec![("inherited.rs", "base"), ("shadowed.rs", "child")]
    );
    assert!(indexed_file_exists(&mut fixture.conn, &ctx, "inherited.rs"));
    assert!(content_chunks_exist(
        &mut fixture.conn,
        &ctx,
        "inherited.rs"
    ));
    assert!(!indexed_file_exists(&mut fixture.conn, &ctx, "deleted.rs"));
    assert!(!content_chunks_exist(&mut fixture.conn, &ctx, "deleted.rs"));
    assert_eq!(
        visible_symbols_for_file(&mut fixture.conn, &ctx, "shadowed.rs")?[0].id,
        owned.id
    );
    assert!(visible_symbols_for_file(&mut fixture.conn, &ctx, "deleted.rs")?.is_empty());
    assert_eq!(
        visible_symbols_for_file(&mut fixture.conn, &ctx, "inherited.rs")?.len(),
        1
    );
    fixture.conn.execute(
        "DELETE FROM code_overlay_pins WHERE overlay_project_id = $1",
        &[&db::id_param(&overlay)?],
    )?;
    assert!(
        !indexed_file_exists(&mut fixture.conn, &ctx, "inherited.rs"),
        "cold overlays inherit nothing"
    );
    assert_eq!(visible_tree(&mut fixture.conn, &ctx)?.len(), 1);
    Ok(())
}

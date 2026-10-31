use super::*;

#[test]
fn symbols_for_file_sql_qualifies_joined_symbol_columns() {
    let sql = symbols_for_files_sql(false, false);

    assert!(sql.contains("SELECT cs.id, cs.project_id, cs.file_path"));
    assert!(sql.contains("FROM code_symbols cs"));
    assert!(sql.contains("JOIN code_indexed_files cf"));
    assert!(sql.contains("cs.file_path = ANY($3)"));
    assert!(!sql.contains("SELECT id, project_id, file_path"));
}

#[test]
fn overlay_symbols_for_files_sql_batches_paths_and_preserves_overlay_shadowing() {
    let sql = symbols_for_files_sql(true, false);

    assert!(sql.contains("SELECT cs.id, cs.project_id, cs.file_path"));
    assert!(sql.contains("cs.file_path = ANY($3)"));
    assert!(sql.contains("fs.project_id = $2"));
    assert!(sql.contains("FROM code_overlay_pins p"));
    assert!(sql.contains("NOT EXISTS"));
    assert!(sql.contains("shadow.project_id = $2"));
    assert!(sql.contains("shadow.file_path = p.file_path"));
    assert!(!sql.contains("cs.file_path = $4"));
}

#[test]
#[cfg_attr(not(gcode_postgres_tests), ignore = "requires PostgreSQL")]
#[serial_test::serial(serial_db)]
fn visible_tree_reads_only_the_context_machine() -> anyhow::Result<()> {
    let mut fixture = pin_fixture::PinFixture::new()?;
    let parent = fixture.parent();
    fixture.version(&parent, "kept.rs", "old", "pinned_original")?;
    fixture.pin(&parent, "kept.rs", "old")?;
    let local = fixture.ctx.clone();
    assert_eq!(
        visible_tree(&mut fixture.conn, &local)?.len(),
        1,
        "the local machine sees its parent rows"
    );
    let foreign = crate::config::Context {
        machine_id: uuid::Uuid::new_v5(
            &crate::models::CODE_INDEX_UUID_NAMESPACE,
            b"visible-tree-foreign-machine",
        )
        .to_string(),
        ..local
    };
    assert!(
        visible_tree(&mut fixture.conn, &foreign)?.is_empty(),
        "a foreign Context reads only its own machine's rows"
    );
    Ok(())
}

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

#[test]
fn with_admitted_versions_binds_the_scope_endpoint_set() {
    use crate::visibility::graph_versions::{AdmittedGraphVersions, with_admitted_versions};

    let admitted = AdmittedGraphVersions {
        keys: vec!["own.rs|h1".to_string()],
        paths: vec!["own.rs".to_string()],
        endpoints: vec!["own.rs|h1".to_string(), "src.rs|h2".to_string()],
    };
    let (_, params) = with_admitted_versions(
        (
            "MATCH (n) RETURN n".to_string(),
            std::collections::HashMap::new(),
        ),
        &admitted,
    );

    assert!(params["admitted_endpoints"].contains("src.rs|h2"));
    assert!(!params["admitted_versions"].contains("src.rs|h2"));
}

#[test]
#[cfg_attr(not(gcode_postgres_tests), ignore = "requires PostgreSQL")]
#[serial_test::serial(serial_db)]
fn scope_endpoints_union_every_namespace_and_exclude_replaced_pins() -> anyhow::Result<()> {
    use crate::visibility::graph_versions::admitted_namespaces;

    let mut fixture = pin_fixture::PinFixture::new()?;
    let source = fixture.source.clone();
    let overlay = fixture.ctx.project_id.clone();
    for path in ["inherited.rs", "shadowed.rs", "deleted.rs"] {
        fixture.version(&source, path, "base", "ancestor")?;
        fixture.pin(&source, path, "base")?;
    }
    fixture.version(&overlay, "shadowed.rs", "child", "child_symbol")?;
    fixture.version(&overlay, "deleted.rs", TOMBSTONE_HASH, "deleted")?;

    let namespaces = admitted_namespaces(&fixture.ctx)?;
    let union: std::collections::BTreeSet<String> = namespaces
        .iter()
        .flat_map(|namespace| namespace.versions.keys.iter().cloned())
        .collect();
    let expected: Vec<String> = union.into_iter().collect();
    for namespace in &namespaces {
        assert_eq!(
            namespace.versions.endpoints, expected,
            "{} must carry the scope-wide endpoint set",
            namespace.project_id
        );
    }

    let endpoints = &namespaces[0].versions.endpoints;
    assert!(endpoints.contains(&"inherited.rs|base".to_string()));
    assert!(endpoints.contains(&"shadowed.rs|child".to_string()));
    assert!(
        !endpoints.contains(&"shadowed.rs|base".to_string()),
        "a pin replaced by the overlay is not an admitted endpoint"
    );
    assert!(
        !endpoints.contains(&"deleted.rs|base".to_string()),
        "a pin tombstoned by the overlay is not an admitted endpoint"
    );
    Ok(())
}

#[test]
#[cfg_attr(not(gcode_postgres_tests), ignore = "requires PostgreSQL")]
#[serial_test::serial(serial_db)]
fn child_call_into_a_source_fresh_pin_carries_the_callee_version() -> anyhow::Result<()> {
    let mut fixture = pin_fixture::PinFixture::new()?;
    let source = fixture.source.clone();
    let overlay = fixture.ctx.project_id.clone();
    let callee = fixture.version(&source, "b.rs", "h2", "callee")?;
    fixture.pin(&source, "b.rs", "h2")?;
    let caller = fixture.version(&overlay, "a.rs", "h1", "caller")?;
    fixture.conn.execute(
        "INSERT INTO code_calls
             (project_id, caller_symbol_id, callee_symbol_id, callee_name, file_path, content_hash, line)
         VALUES ($1, $2, $3, 'callee', 'a.rs', 'h1', 3)",
        &[
            &db::id_param(&overlay)?,
            &db::id_param(&caller.id)?,
            &db::id_param(&callee.id)?,
        ],
    )?;

    let facts = db::read_version_graph_facts(&mut fixture.conn, &overlay, "a.rs", "h1")?;
    let call = facts.calls.first().expect("the child caller's call");

    assert_eq!(call.callee_symbol_id.as_deref(), Some(callee.id.as_str()));
    assert_eq!(call.callee_file_path.as_deref(), Some("b.rs"));
    assert_eq!(call.callee_file_content_hash.as_deref(), Some("h2"));
    Ok(())
}

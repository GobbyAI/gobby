use super::*;
use crate::index::api;
use crate::models::{
    CallTargetKind, HeritageKind, IndexedFile, IndexedProject, InheritanceRelation,
};
use std::time::{SystemTime, UNIX_EPOCH};

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn mark_graph_synced_cas_rejects_stale_hash() {
    let (mut conn, project_id, _cleanup) = seeded_file("gcode-cas-stale-hash", "hash-a");
    let attempt = mark_graph_sync_attempted(&mut conn, &project_id, "src/lib.rs")
        .expect("attempt")
        .expect("live row");
    assert_eq!(attempt.content_hash, "hash-a");

    upsert_content_version(&mut conn, &project_id, "hash-b");
    point_file_state(&mut conn, &project_id, "hash-b");

    let succeeded = mark_graph_synced(
        &mut conn,
        &project_id,
        "src/lib.rs",
        &attempt.content_hash,
        attempt.attempted_at,
    )
    .expect("stale cas");
    assert!(!succeeded);
    let (synced, attempt_cleared) = graph_sync_flags(&mut conn, &project_id, "hash-b");
    assert!(!synced, "late completion must not mark the new hash synced");
    assert!(attempt_cleared);
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn mark_graph_synced_failed_cas_dirties_live_row() {
    let (mut conn, project_id, _cleanup) = seeded_file("gcode-cas-dirty-live", "hash-a");
    let h1 = mark_graph_sync_attempted(&mut conn, &project_id, "src/lib.rs")
        .expect("h1 attempt")
        .expect("live row");

    upsert_content_version(&mut conn, &project_id, "hash-b");
    point_file_state(&mut conn, &project_id, "hash-b");
    let h2 = mark_graph_sync_attempted(&mut conn, &project_id, "src/lib.rs")
        .expect("h2 attempt")
        .expect("live row");
    assert!(
        mark_graph_synced(
            &mut conn,
            &project_id,
            "src/lib.rs",
            &h2.content_hash,
            h2.attempted_at,
        )
        .expect("h2 cas")
    );

    let succeeded = mark_graph_synced(
        &mut conn,
        &project_id,
        "src/lib.rs",
        &h1.content_hash,
        h1.attempted_at,
    )
    .expect("late h1 cas");
    assert!(!succeeded);
    let (synced, attempt_cleared) = graph_sync_flags(&mut conn, &project_id, "hash-b");
    assert!(!synced);
    assert!(attempt_cleared);
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn mark_graph_synced_cas_rejects_same_hash_stale_attempt() {
    let (mut conn, project_id, _cleanup) = seeded_file("gcode-cas-same-hash", "hash-b");
    let h1 = mark_graph_sync_attempted(&mut conn, &project_id, "src/lib.rs")
        .expect("h1 attempt")
        .expect("live row");
    dirty_graph_sync_for_file(&mut conn, &project_id, "src/lib.rs", "hash-b")
        .expect("promotion dirty");
    let h2 = mark_graph_sync_attempted(&mut conn, &project_id, "src/lib.rs")
        .expect("h2 attempt")
        .expect("live row");
    assert_eq!(h1.content_hash, h2.content_hash);
    assert!(
        mark_graph_synced(
            &mut conn,
            &project_id,
            "src/lib.rs",
            &h2.content_hash,
            h2.attempted_at,
        )
        .expect("h2 cas")
    );

    let succeeded = mark_graph_synced(
        &mut conn,
        &project_id,
        "src/lib.rs",
        &h1.content_hash,
        h1.attempted_at,
    )
    .expect("late h1 same-hash cas");
    assert!(!succeeded);
    let (synced, attempt_cleared) = graph_sync_flags(&mut conn, &project_id, "hash-b");
    assert!(!synced);
    assert!(attempt_cleared);
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn read_graph_file_facts_includes_inheritance_rows() {
    let (mut conn, project_id, _cleanup) = seeded_file("gcode-facts-inherit", "hash-a");
    api::upsert_inheritance(
        &mut conn,
        &project_id,
        "src/lib.rs",
        "hash-a",
        &[InheritanceRelation {
            source_symbol_id: None,
            source_name: "Derived".to_string(),
            source_kind: CallTargetKind::Unresolved,
            source_external_module: None,
            target_symbol_id: None,
            target_name: "Base".to_string(),
            target_kind: CallTargetKind::Unresolved,
            target_external_module: None,
            heritage_kind: HeritageKind::Extends,
            file_path: "src/lib.rs".to_string(),
            content_hash: "hash-a".to_string(),
            line: 3,
        }],
    )
    .expect("upsert inheritance");
    conn.execute(
        "INSERT INTO code_imports (project_id, source_file, content_hash, target_module)
         VALUES ($1, 'src/lib.rs', 'hash-a', 'pkg.mod')",
        &[&id_param(&project_id).expect("uuid")],
    )
    .expect("insert import");

    let facts = read_graph_file_facts(&mut conn, &project_id, "src/lib.rs").expect("facts");
    assert!(facts.definitions.is_empty() && facts.calls.is_empty());
    assert_eq!(facts.imports.len(), 1);
    assert_eq!(facts.inheritance.len(), 1);
    assert_eq!(facts.inheritance[0].target_name, "Base");
    assert_eq!(facts.inheritance[0].heritage_kind, HeritageKind::Extends);

    let pairs = read_active_imports(&mut conn, &project_id).expect("active imports");
    assert_eq!(pairs.len(), 1);
    assert_eq!(pairs[0].file_path, "src/lib.rs");
    assert_eq!(pairs[0].module_name, "pkg.mod");
}

struct ProjectCleanup {
    database_url: String,
    project_id: String,
}

impl Drop for ProjectCleanup {
    fn drop(&mut self) {
        if let Ok(mut conn) = crate::db::connect_readwrite(&self.database_url) {
            let _ = cleanup(&mut conn, &self.project_id);
        }
    }
}

fn seeded_file(prefix: &str, hash: &str) -> (postgres::Client, String, ProjectCleanup) {
    let database_url = crate::test_env::postgres_test_database_url("queries CAS tests");
    let mut conn = crate::db::connect_readwrite(&database_url).expect("connect");
    let project_id = unique_id(prefix);
    cleanup(&mut conn, &project_id).expect("pre-clean");
    let cleanup_guard = ProjectCleanup {
        database_url,
        project_id: project_id.clone(),
    };
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine");
    api::upsert_project_stats(
        &mut conn,
        &machine_id,
        &IndexedProject {
            id: project_id.clone(),
            root_path: format!("/tmp/{project_id}"),
            total_files: 1,
            total_symbols: 0,
            last_indexed_at: String::new(),
            index_duration_ms: 0,
            total_eligible_files: None,
        },
    )
    .expect("seed project");
    upsert_content_version(&mut conn, &project_id, hash);
    point_file_state(&mut conn, &project_id, hash);
    (conn, project_id, cleanup_guard)
}

fn upsert_content_version(conn: &mut postgres::Client, project_id: &str, hash: &str) {
    let rel = "src/lib.rs";
    api::upsert_file(
        conn,
        &IndexedFile {
            id: IndexedFile::make_id(project_id, rel, hash),
            project_id: project_id.to_string(),
            file_path: rel.to_string(),
            language: "rust".to_string(),
            content_hash: hash.to_string(),
            symbol_count: 0,
            byte_size: 16,
            indexed_at: String::new(),
        },
    )
    .expect("upsert content version");
}

fn point_file_state(conn: &mut postgres::Client, project_id: &str, hash: &str) {
    let machine_id = gobby_core::machine::read_local_machine_id().expect("machine");
    api::upsert_file_state(
        conn,
        &machine_id,
        &IndexedFile {
            id: IndexedFile::make_id(project_id, "src/lib.rs", hash),
            project_id: project_id.to_string(),
            file_path: "src/lib.rs".to_string(),
            language: "rust".to_string(),
            content_hash: hash.to_string(),
            symbol_count: 0,
            byte_size: 16,
            indexed_at: String::new(),
        },
    )
    .expect("point file state");
}

fn graph_sync_flags(conn: &mut postgres::Client, project_id: &str, hash: &str) -> (bool, bool) {
    let project_uuid = id_param(project_id).expect("uuid");
    let row = conn
        .query_one(
            "SELECT graph_synced, graph_sync_attempted_at IS NULL
             FROM code_indexed_files
             WHERE project_id = $1 AND file_path = 'src/lib.rs' AND content_hash = $2",
            &[&project_uuid, &hash],
        )
        .expect("load flags");
    (row.get(0), row.get(1))
}

fn cleanup(conn: &mut postgres::Client, project_id: &str) -> anyhow::Result<()> {
    let project_id = id_param(project_id)?;
    conn.execute(
        "DELETE FROM code_indexed_file_states WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_indexed_project_states WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_inheritance WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_calls WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_imports WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_content_chunks WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_symbols WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_indexed_files WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM code_indexed_projects WHERE id = $1",
        &[&project_id],
    )?;
    Ok(())
}

fn unique_id(prefix: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("epoch")
        .as_nanos();
    uuid::Uuid::new_v5(
        &crate::models::CODE_INDEX_UUID_NAMESPACE,
        format!("{prefix}-{}-{nanos}", std::process::id()).as_bytes(),
    )
    .to_string()
}

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
fn dirty_graph_sync_for_unreferenced_hash_does_not_change_active_state() {
    let (mut conn, project_id, _cleanup) = seeded_file("gcode-cas-unref-hash", "hash-a");
    let attempt = mark_graph_sync_attempted(&mut conn, &project_id, "src/lib.rs")
        .expect("attempt")
        .expect("live row");
    assert!(
        mark_graph_synced(
            &mut conn,
            &project_id,
            "src/lib.rs",
            &attempt.content_hash,
            attempt.attempted_at,
        )
        .expect("sync active hash")
    );
    upsert_content_version(&mut conn, &project_id, "hash-unreferenced");
    let (synced_before, attempt_cleared_before) =
        graph_sync_flags(&mut conn, &project_id, "hash-a");
    dirty_graph_sync_for_file(&mut conn, &project_id, "src/lib.rs", "hash-unreferenced")
        .expect("unreferenced dirty");
    let (synced_after, attempt_cleared_after) = graph_sync_flags(&mut conn, &project_id, "hash-a");
    assert_eq!(synced_before, synced_after);
    assert_eq!(attempt_cleared_before, attempt_cleared_after);
    assert!(synced_after);
    assert!(
        !attempt_cleared_after,
        "a successful CAS keeps graph_sync_attempted_at as the last-attempt marker"
    );
    let (unref_synced, unref_cleared) =
        graph_sync_flags(&mut conn, &project_id, "hash-unreferenced");
    assert!(!unref_synced);
    assert!(unref_cleared);
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
    assert_ne!(
        h1.attempted_at, h2.attempted_at,
        "stale CAS requires distinct attempt timestamps"
    );
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

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn mark_vectors_synced_cas_marks_attempted_version() {
    let (mut conn, project_id, _cleanup) = seeded_file("gcode-vector-cas-marks", "hash-a");
    let attempt = mark_vector_sync_attempted(&mut conn, &project_id, "src/lib.rs")
        .expect("attempt")
        .expect("live row");
    assert_eq!(attempt.content_hash, "hash-a");
    assert_eq!(
        vector_sync_state(&mut conn, &project_id, "hash-a"),
        (false, Some(attempt.attempted_at))
    );

    let succeeded = mark_vectors_synced(
        &mut conn,
        &project_id,
        "src/lib.rs",
        &attempt.content_hash,
        attempt.attempted_at,
    )
    .expect("cas");
    assert!(succeeded);
    assert_eq!(
        vector_sync_state(&mut conn, &project_id, "hash-a"),
        (true, Some(attempt.attempted_at)),
        "completion marks the attempted version synced and keeps its attempt time"
    );
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn mark_vectors_synced_cas_rejects_completion_after_state_moves() {
    let (mut conn, project_id, _cleanup) = seeded_file("gcode-vector-cas-moved", "hash-a");
    let attempt = mark_vector_sync_attempted(&mut conn, &project_id, "src/lib.rs")
        .expect("attempt")
        .expect("live row");

    upsert_content_version(&mut conn, &project_id, "hash-b");
    point_file_state(&mut conn, &project_id, "hash-b");

    let succeeded = mark_vectors_synced(
        &mut conn,
        &project_id,
        "src/lib.rs",
        &attempt.content_hash,
        attempt.attempted_at,
    )
    .expect("stale cas");
    assert!(!succeeded);
    let (old_synced, _) = vector_sync_state(&mut conn, &project_id, "hash-a");
    assert!(
        !old_synced,
        "late completion must not mark the old hash synced"
    );
    assert_eq!(
        vector_sync_state(&mut conn, &project_id, "hash-b"),
        (false, None),
        "late completion must leave the new current version pending"
    );
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn mark_vectors_synced_cas_rejects_same_hash_stale_attempt() {
    let (mut conn, project_id, _cleanup) = seeded_file("gcode-vector-cas-same-hash", "hash-b");
    let h1 = mark_vector_sync_attempted(&mut conn, &project_id, "src/lib.rs")
        .expect("h1 attempt")
        .expect("live row");
    let h2 = mark_vector_sync_attempted(&mut conn, &project_id, "src/lib.rs")
        .expect("h2 attempt")
        .expect("live row");
    assert_eq!(h1.content_hash, h2.content_hash);
    assert_ne!(
        h1.attempted_at, h2.attempted_at,
        "stale CAS requires distinct attempt timestamps"
    );
    assert!(
        mark_vectors_synced(
            &mut conn,
            &project_id,
            "src/lib.rs",
            &h2.content_hash,
            h2.attempted_at,
        )
        .expect("h2 cas")
    );

    let succeeded = mark_vectors_synced(
        &mut conn,
        &project_id,
        "src/lib.rs",
        &h1.content_hash,
        h1.attempted_at,
    )
    .expect("late h1 same-hash cas");
    assert!(!succeeded);
    assert_eq!(
        vector_sync_state(&mut conn, &project_id, "hash-b"),
        (false, None)
    );
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn mark_vectors_synced_cas_failure_dirties_attempted_version() {
    for h_referenced in [true, false] {
        assert_eq!(
            stale_completion(Projection::Vectors, h_referenced),
            BOTH_VERSIONS_DIRTIED,
            "h_referenced={h_referenced}"
        );
    }
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn mark_graph_synced_cas_failure_dirties_attempted_version() {
    for h_referenced in [true, false] {
        assert_eq!(
            stale_completion(Projection::Graph, h_referenced),
            BOTH_VERSIONS_DIRTIED,
            "h_referenced={h_referenced}"
        );
    }
}

#[derive(Debug, Clone, Copy)]
enum Projection {
    Vectors,
    Graph,
}

impl Projection {
    fn synced_column(self) -> &'static str {
        match self {
            Self::Vectors => "vectors_synced",
            Self::Graph => "graph_synced",
        }
    }

    fn attempted_column(self) -> &'static str {
        match self {
            Self::Vectors => "vector_sync_attempted_at",
            Self::Graph => "graph_sync_attempted_at",
        }
    }
}

/// S9: this machine's state moved from H to K while its H write was in
/// flight, and another writer re-synced H under a later attempt. The stale
/// write may have overwritten that newer projection of H, so its failed
/// completion must leave both H and K pending, whether or not any state
/// still references H.
fn stale_completion(projection: Projection, h_referenced: bool) -> StaleCompletion {
    let prefix = format!("gcode-cas-s9-{projection:?}-{h_referenced}");
    let (mut conn, project_id, _cleanup) = seeded_file(&prefix, "hash-h");
    let (attempt_hash, attempted_at) = match projection {
        Projection::Vectors => {
            let attempt = mark_vector_sync_attempted(&mut conn, &project_id, "src/lib.rs")
                .expect("vector attempt")
                .expect("live row");
            (attempt.content_hash, attempt.attempted_at)
        }
        Projection::Graph => {
            let attempt = mark_graph_sync_attempted(&mut conn, &project_id, "src/lib.rs")
                .expect("graph attempt")
                .expect("live row");
            (attempt.content_hash, attempt.attempted_at)
        }
    };
    let _other_machine =
        h_referenced.then(|| OtherMachineState::referencing(&mut conn, &project_id, "hash-h"));

    upsert_content_version(&mut conn, &project_id, "hash-k");
    point_file_state(&mut conn, &project_id, "hash-k");
    let project_uuid = id_param(&project_id).expect("uuid");
    conn.execute(
        &format!(
            "UPDATE code_indexed_files
             SET {synced} = true, {attempted} = NOW()
             WHERE project_id = $1 AND file_path = 'src/lib.rs' AND content_hash = 'hash-h'",
            synced = projection.synced_column(),
            attempted = projection.attempted_column(),
        ),
        &[&project_uuid],
    )
    .expect("re-sync H under a later attempt");

    let succeeded = match projection {
        Projection::Vectors => mark_vectors_synced(
            &mut conn,
            &project_id,
            "src/lib.rs",
            &attempt_hash,
            attempted_at,
        ),
        Projection::Graph => mark_graph_synced(
            &mut conn,
            &project_id,
            "src/lib.rs",
            &attempt_hash,
            attempted_at,
        ),
    }
    .expect("stale cas");
    StaleCompletion {
        succeeded,
        h: projection_flags(&mut conn, &project_id, "hash-h", projection),
        k: projection_flags(&mut conn, &project_id, "hash-k", projection),
    }
}

/// A failed completion that leaves H and K pending with no attempt.
const BOTH_VERSIONS_DIRTIED: StaleCompletion = StaleCompletion {
    succeeded: false,
    h: (false, true),
    k: (false, true),
};

/// Outcome of a stale completion: whether it succeeded, then the
/// `(synced, attempt_is_null)` flags of H and K.
#[derive(Debug, PartialEq, Eq)]
struct StaleCompletion {
    succeeded: bool,
    h: (bool, bool),
    k: (bool, bool),
}

/// Another machine's file state for `src/lib.rs`. Dropping it removes that
/// machine's rows before the project cleanup runs.
struct OtherMachineState {
    database_url: String,
    machine_id: String,
}

impl OtherMachineState {
    fn referencing(conn: &mut postgres::Client, project_id: &str, hash: &str) -> Self {
        let database_url = crate::test_env::postgres_test_database_url("queries CAS tests");
        let machine_id = uuid::Uuid::new_v4().to_string();
        crate::test_env::seed_test_machine(conn, &machine_id).expect("seed other machine");
        let guard = Self {
            database_url,
            machine_id: machine_id.clone(),
        };
        api::upsert_project_stats(
            conn,
            &machine_id,
            &IndexedProject {
                id: project_id.to_string(),
                root_path: format!("/tmp/other-{project_id}"),
                total_files: 1,
                total_symbols: 0,
                last_indexed_at: String::new(),
                index_duration_ms: 0,
                total_eligible_files: None,
                indexer_version: None,
            },
            api::IndexWriteMode::Overlay,
            true,
        )
        .expect("seed other machine project state");
        api::file_state::upsert_file_state(
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
            std::path::Path::new("/tmp/queries-cas-other"),
            api::IndexWriteMode::Overlay,
        )
        .expect("point other machine file state");
        guard
    }
}

impl Drop for OtherMachineState {
    fn drop(&mut self) {
        let Ok(mut conn) = crate::db::connect_readwrite(&self.database_url) else {
            return;
        };
        let Ok(machine_id) = id_param(&self.machine_id) else {
            return;
        };
        for statement in [
            "DELETE FROM code_indexed_file_states WHERE machine_id = $1",
            "DELETE FROM code_indexed_project_states WHERE machine_id = $1",
            "DELETE FROM machines WHERE id = $1",
        ] {
            let _ = conn.execute(statement, &[&machine_id]);
        }
    }
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
            indexer_version: None,
        },
        api::IndexWriteMode::Overlay,
        true,
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
    api::file_state::upsert_file_state(
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
        std::path::Path::new("/tmp/queries-cas"),
        api::IndexWriteMode::Overlay,
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

fn vector_sync_state(
    conn: &mut postgres::Client,
    project_id: &str,
    hash: &str,
) -> (bool, Option<SystemTime>) {
    let project_uuid = id_param(project_id).expect("uuid");
    let row = conn
        .query_one(
            "SELECT vectors_synced, vector_sync_attempted_at
             FROM code_indexed_files
             WHERE project_id = $1 AND file_path = 'src/lib.rs' AND content_hash = $2",
            &[&project_uuid, &hash],
        )
        .expect("load vector state");
    (row.get(0), row.get(1))
}

/// Returns `(synced, attempt_is_null)` for the projection's flag pair.
fn projection_flags(
    conn: &mut postgres::Client,
    project_id: &str,
    hash: &str,
    projection: Projection,
) -> (bool, bool) {
    let project_uuid = id_param(project_id).expect("uuid");
    let row = conn
        .query_one(
            &format!(
                "SELECT {synced}, {attempted} IS NULL
                 FROM code_indexed_files
                 WHERE project_id = $1 AND file_path = 'src/lib.rs' AND content_hash = $2",
                synced = projection.synced_column(),
                attempted = projection.attempted_column(),
            ),
            &[&project_uuid, &hash],
        )
        .expect("load projection flags");
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

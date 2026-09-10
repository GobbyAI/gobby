use std::time::{SystemTime, UNIX_EPOCH};

use postgres::GenericClient;

use crate::db;
use crate::models::{
    CODE_INDEX_UUID_NAMESPACE, CallRelation, CallTargetKind, IndexedFile, IndexedProject, Symbol,
};

use super::*;

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn upsert_calls_batches_commands_and_preserves_row_semantics() {
    let database_url = crate::test_env::postgres_test_database_url("call batch SQL test");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)
        .expect("connect to PostgreSQL test database");
    let project_id = unique_test_project_id("gcode-api-call-batches");
    let project_uuid = db::id_param(&project_id).expect("test project id is a uuid");
    let rel = "src/calls.rs";
    let mut tx = conn.transaction().expect("start call batch transaction");
    seed_project(&mut tx, &project_id);
    upsert_file(&mut tx, &indexed_file(&project_id, rel, "hash-a"))
        .expect("seed first indexed file version");
    upsert_file(&mut tx, &indexed_file(&project_id, rel, "hash-b"))
        .expect("seed second indexed file version");

    let stale = CallRelation::new(String::new(), "stale".to_string(), rel.to_string(), 1);
    upsert_calls(&mut tx, &project_id, rel, "hash-a", &[stale])
        .expect("seed row replaced for the same file and hash");
    let other_hash = CallRelation::new(String::new(), "other_hash".to_string(), rel.to_string(), 2);
    upsert_calls(&mut tx, &project_id, rel, "hash-b", &[other_hash])
        .expect("seed row retained for a different content hash");

    tx.batch_execute(
        "CREATE TEMP TABLE call_insert_statements (marker boolean);
         CREATE FUNCTION pg_temp.count_call_insert_statements() RETURNS trigger
         LANGUAGE plpgsql AS $function$
         BEGIN
             INSERT INTO call_insert_statements VALUES (TRUE);
             RETURN NULL;
         END
         $function$;",
    )
    .expect("create transaction-scoped command counter");
    let trigger_name = format!("count_call_inserts_{}", project_uuid.simple());
    tx.batch_execute(&format!(
        "CREATE TRIGGER {trigger_name}
         AFTER INSERT ON code_calls
         FOR EACH STATEMENT EXECUTE FUNCTION pg_temp.count_call_insert_statements()"
    ))
    .expect("attach statement-level command counter");

    let missing = CallRelation {
        caller_symbol_id: String::new(),
        callee_symbol_id: None,
        callee_name: "shared".to_string(),
        callee_target_kind: CallTargetKind::Unresolved,
        callee_external_module: None,
        file_path: rel.to_string(),
        content_hash: "ignored-domain-hash".to_string(),
        line: 11,
    };
    let external_a = CallRelation {
        callee_target_kind: CallTargetKind::External,
        callee_external_module: Some("crate_a".to_string()),
        ..missing.clone()
    };
    let external_b = CallRelation {
        callee_external_module: Some("crate_b".to_string()),
        ..external_a.clone()
    };
    let local_import = CallRelation {
        callee_target_kind: CallTargetKind::LocalImport,
        callee_external_module: Some("crate_a".to_string()),
        ..missing.clone()
    };
    let caller_id = Symbol::make_id(&project_id, rel, "hash-a", "caller", "function", 0);
    let callee_id = Symbol::make_id(&project_id, rel, "hash-a", "callee", "function", 20);
    let symbol = CallRelation {
        caller_symbol_id: caller_id.clone(),
        callee_symbol_id: Some(callee_id.clone()),
        callee_target_kind: CallTargetKind::Symbol,
        ..missing.clone()
    };
    let mut replacement = vec![
        missing.clone(),
        external_a,
        external_b,
        local_import,
        symbol,
    ];
    replacement.extend((0..495).map(|index| {
        CallRelation::new(
            String::new(),
            format!("filler_{index:03}"),
            rel.to_string(),
            100 + index,
        )
    }));
    replacement.push(CallRelation {
        callee_symbol_id: Some(String::new()),
        ..missing
    });

    tx.execute("TRUNCATE call_insert_statements", &[])
        .expect("reset command counter");
    assert_eq!(
        upsert_calls(&mut tx, &project_id, rel, "hash-a", &replacement)
            .expect("replace calls in bounded batches"),
        500
    );
    let insert_statements: i64 = tx
        .query_one("SELECT COUNT(*) FROM call_insert_statements", &[])
        .expect("count insert statements")
        .get(0);
    assert_eq!(insert_statements, 2);

    let shared_rows = tx
        .query(
            "SELECT caller_symbol_id::text, callee_symbol_id::text, callee_name,
                    callee_target_kind, callee_external_module, file_path, content_hash, line
             FROM code_calls
             WHERE project_id = $1 AND content_hash = 'hash-a' AND callee_name = 'shared'
             ORDER BY callee_target_kind, callee_external_module",
            &[&project_uuid],
        )
        .expect("read representative call rows")
        .into_iter()
        .map(|row| {
            (
                row.get::<_, Option<String>>(0),
                row.get::<_, Option<String>>(1),
                row.get::<_, String>(2),
                row.get::<_, String>(3),
                row.get::<_, String>(4),
                row.get::<_, String>(5),
                row.get::<_, String>(6),
                row.get::<_, i32>(7),
            )
        })
        .collect::<Vec<_>>();
    assert_eq!(
        shared_rows,
        vec![
            (
                None, None, "shared", "external", "crate_a", rel, "hash-a", 11
            ),
            (
                None, None, "shared", "external", "crate_b", rel, "hash-a", 11
            ),
            (
                None,
                None,
                "shared",
                "local_import",
                "crate_a",
                rel,
                "hash-a",
                11
            ),
            (
                Some(caller_id),
                Some(callee_id),
                "shared",
                "symbol",
                "",
                rel,
                "hash-a",
                11,
            ),
            (None, None, "shared", "unresolved", "", rel, "hash-a", 11),
        ]
        .into_iter()
        .map(|(caller, callee, name, kind, module, file, hash, line)| {
            (
                caller,
                callee,
                name.to_string(),
                kind.to_string(),
                module.to_string(),
                file.to_string(),
                hash.to_string(),
                line,
            )
        })
        .collect::<Vec<_>>()
    );

    let (stale_count, other_hash_count, filler_count): (i64, i64, i64) = tx
        .query_one(
            "SELECT
                COUNT(*) FILTER (WHERE callee_name = 'stale'),
                COUNT(*) FILTER (WHERE content_hash = 'hash-b' AND callee_name = 'other_hash'),
                COUNT(*) FILTER (WHERE content_hash = 'hash-a' AND callee_name LIKE 'filler_%')
             FROM code_calls
             WHERE project_id = $1 AND file_path = $2",
            &[&project_uuid, &rel],
        )
        .map(|row| (row.get(0), row.get(1), row.get(2)))
        .expect("verify replacement scope");
    assert_eq!((stale_count, other_hash_count, filler_count), (0, 1, 495));

    assert_eq!(
        upsert_calls(&mut tx, &project_id, rel, "hash-a", &[])
            .expect("replace calls with an empty set"),
        0
    );
    let (empty_hash_count, retained_hash_count): (i64, i64) = tx
        .query_one(
            "SELECT
                COUNT(*) FILTER (WHERE content_hash = 'hash-a'),
                COUNT(*) FILTER (WHERE content_hash = 'hash-b')
             FROM code_calls
             WHERE project_id = $1 AND file_path = $2",
            &[&project_uuid, &rel],
        )
        .map(|row| (row.get(0), row.get(1)))
        .expect("verify empty replacement scope");
    assert_eq!((empty_hash_count, retained_hash_count), (0, 1));

    tx.rollback().expect("rollback call batch transaction");
}

#[test]
#[serial_test::serial(serial_db)]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
fn caller_transaction_rolls_back_batches_before_late_invalid_uuid() {
    let database_url = crate::test_env::postgres_test_database_url("call batch rollback SQL test");
    let mut conn = gobby_core::postgres::connect_readwrite(&database_url)
        .expect("connect to PostgreSQL test database");
    let project_id = unique_test_project_id("gcode-api-call-rollback");
    cleanup_project(&mut conn, &project_id).expect("pre-clean test project rows");
    let _cleanup = ProjectCleanup {
        database_url,
        project_id: project_id.clone(),
    };
    seed_project(&mut conn, &project_id);
    let rel = "src/rollback.rs";
    upsert_file(&mut conn, &indexed_file(&project_id, rel, "hash-a")).expect("seed indexed file");
    let preexisting =
        CallRelation::new(String::new(), "preexisting".to_string(), rel.to_string(), 1);
    upsert_calls(&mut conn, &project_id, rel, "hash-a", &[preexisting])
        .expect("seed preexisting call");

    let mut replacement = (0..500)
        .map(|index| {
            CallRelation::new(
                String::new(),
                format!("replacement_{index:03}"),
                rel.to_string(),
                index,
            )
        })
        .collect::<Vec<_>>();
    replacement.push(CallRelation::new(
        "not-a-uuid".to_string(),
        "invalid".to_string(),
        rel.to_string(),
        501,
    ));

    let project_uuid = db::id_param(&project_id).expect("test project id is a uuid");
    let mut tx = conn.transaction().expect("start caller-owned transaction");
    let error = upsert_calls(&mut tx, &project_id, rel, "hash-a", &replacement)
        .expect_err("late invalid UUID must fail the replacement");
    assert!(error.to_string().contains("invalid uuid id `not-a-uuid`"));
    let partial_count: i64 = tx
        .query_one(
            "SELECT COUNT(*) FROM code_calls WHERE project_id = $1 AND content_hash = 'hash-a'",
            &[&project_uuid],
        )
        .expect("count rows written before the late error")
        .get(0);
    assert_eq!(partial_count, 500);
    tx.rollback().expect("rollback caller-owned transaction");

    let retained_names = conn
        .query(
            "SELECT callee_name FROM code_calls
             WHERE project_id = $1 AND content_hash = 'hash-a'
             ORDER BY callee_name",
            &[&project_uuid],
        )
        .expect("read calls after rollback")
        .into_iter()
        .map(|row| row.get::<_, String>(0))
        .collect::<Vec<_>>();
    assert_eq!(retained_names, vec!["preexisting"]);
}

fn indexed_file(project_id: &str, file_path: &str, content_hash: &str) -> IndexedFile {
    IndexedFile {
        id: IndexedFile::make_id(project_id, file_path, content_hash),
        project_id: project_id.to_string(),
        file_path: file_path.to_string(),
        language: "rust".to_string(),
        content_hash: content_hash.to_string(),
        symbol_count: 0,
        byte_size: 16,
        indexed_at: String::new(),
    }
}

fn unique_test_project_id(prefix: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is after unix epoch")
        .as_nanos();
    uuid::Uuid::new_v5(
        &CODE_INDEX_UUID_NAMESPACE,
        format!("{prefix}-{nanos}").as_bytes(),
    )
    .to_string()
}

fn seed_project(conn: &mut impl GenericClient, project_id: &str) {
    let machine_id = gobby_core::machine::read_local_machine_id().expect("read machine id");
    upsert_project_stats(
        conn,
        &machine_id,
        &IndexedProject {
            id: project_id.to_string(),
            root_path: format!("/tmp/{project_id}"),
            total_files: 1,
            total_symbols: 1,
            last_indexed_at: String::new(),
            index_duration_ms: 0,
            total_eligible_files: None,
            indexer_version: None,
        },
        IndexWriteMode::Overlay,
        true,
    )
    .expect("seed project row");
}

struct ProjectCleanup {
    database_url: String,
    project_id: String,
}

impl Drop for ProjectCleanup {
    fn drop(&mut self) {
        if let Ok(mut conn) = gobby_core::postgres::connect_readwrite(&self.database_url) {
            let _ = cleanup_project(&mut conn, &self.project_id);
        }
    }
}

fn cleanup_project(conn: &mut impl GenericClient, project_id: &str) -> anyhow::Result<()> {
    let project_id = db::id_param(project_id)?;
    for table in [
        "code_indexed_file_states",
        "code_indexed_project_states",
        "code_calls",
        "code_inheritance",
        "code_imports",
        "code_content_chunks",
        "code_symbols",
        "code_indexed_files",
    ] {
        conn.execute(
            &format!("DELETE FROM {table} WHERE project_id = $1"),
            &[&project_id],
        )?;
    }
    conn.execute(
        "DELETE FROM code_indexed_projects WHERE id = $1",
        &[&project_id],
    )?;
    conn.execute(
        "DELETE FROM project_checkouts WHERE project_id = $1",
        &[&project_id],
    )?;
    conn.execute("DELETE FROM projects WHERE id = $1", &[&project_id])?;
    Ok(())
}

use std::path::Path;
use std::sync::mpsc;
use std::time::Duration;

use super::*;

/// The refresh connection gives up on a lock after about 5s; this deadline only
/// has to outlast that window on a slow host before the test calls it a hang.
const REFRESH_DEADLINE: Duration = Duration::from_secs(30);

fn bounded_refresh_context(root: &Path) -> Context {
    let root = root
        .canonicalize()
        .expect("canonical freshness test checkout");
    let database_url = crate::test_env::postgres_test_database_url("freshness refresh bound tests");
    let project_id = uuid::Uuid::new_v5(
        &crate::models::CODE_INDEX_UUID_NAMESPACE,
        b"gcode-freshness-checkout-row-lock",
    )
    .to_string();
    let mut conn =
        db::connect_readwrite(&database_url).expect("connect freshness PostgreSQL test database");
    crate::test_env::seed_test_checkout(&mut conn, &project_id, &root)
        .expect("register freshness test checkout");
    Context {
        database_url,
        project_root: root,
        project_id,
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
        index_scope: ProjectIndexScope::Single,
    }
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires a PostgreSQL test database URL"
)]
#[serial_test::serial(serial_db)]
fn refresh_gives_up_on_a_locked_checkout_row() {
    let tmp = tempfile::tempdir().expect("tempdir");
    std::fs::write(tmp.path().join("lib.rs"), b"pub fn one() {}\n").expect("write source");
    let ctx = bounded_refresh_context(tmp.path());

    // Hold the checkout row that the refresh must share-lock (FOR SHARE in
    // upsert_project_seed). Until this transaction ends, the refresh's write
    // waits on it.
    let mut holder = db::connect_readwrite(&ctx.database_url).expect("connect row-lock holder");
    let mut transaction = holder.transaction().expect("begin row-lock holder");
    let machine_id = uuid::Uuid::parse_str(&ctx.machine_id).expect("machine id is a uuid");
    let project_id = uuid::Uuid::parse_str(&ctx.project_id).expect("project id is a uuid");
    let root = ctx.project_root.to_string_lossy().to_string();
    transaction
        .execute(
            "SELECT 1 FROM project_checkouts
             WHERE machine_id = $1 AND project_id = $2 AND root_path = $3
             FOR UPDATE",
            &[&machine_id, &project_id, &root],
        )
        .expect("lock the checkout row");

    let (sender, receiver) = mpsc::channel();
    let refresh_ctx = ctx.clone();
    std::thread::spawn(move || {
        let _ = sender.send(ensure_fresh(&refresh_ctx, FreshnessScope::Project));
    });
    let status = receiver
        .recv_timeout(REFRESH_DEADLINE)
        .expect("the refresh must give up on the locked checkout row instead of waiting forever")
        .expect("a lock timeout degrades the refresh");

    match &status {
        FreshnessStatus::Degraded(message) => assert!(
            message.contains("lock timeout"),
            "the degraded reason must name the lock timeout: {message}"
        ),
        other => panic!("a lock timeout must degrade the refresh, got {other:?}"),
    }
    transaction
        .rollback()
        .expect("release the checkout row lock");
}

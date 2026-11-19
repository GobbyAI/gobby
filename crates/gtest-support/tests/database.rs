//! Per-run database lifecycle on the test hub (design-v2 section 7.2). Tests that touch the hub
//! skip with a printed line when DATABASE_URL is unset (ruling H18); the sweep refusals need no hub.

use std::future::Future;
use std::io::ErrorKind;
use std::net::TcpListener;
use std::panic::AssertUnwindSafe;
use std::process::Command;
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tokio_postgres::NoTls;

use gobby_test_support::database::{
    DatabaseError, create_database, database_exists, drop_database, new_run_suffix,
    start_run_database, sweep_dead_pid,
};
use gobby_test_support::hub_guard::{
    DbUrlError, GuardMode, HARNESS_DATABASE_PREFIX, HubGuard, SHARED_PORTS, check_test_hub,
    database_url_for_tests, derive_run_url, harness_database_name, maintenance_url,
    run_database_pid,
};

use super::guard::materialize_profile;

/// The hub URL, or `None` after printing why the test is skipped (ruling H18).
fn test_hub_url() -> Option<String> {
    match database_url_for_tests() {
        Ok(url) => Some(url),
        Err(DbUrlError::Unset) => {
            println!("skipping: DATABASE_URL is unset, so the test hub is not available");
            None
        }
        Err(DbUrlError::Refused(error)) => panic!("DATABASE_URL is refused: {error}"),
    }
}

fn strict_guard(url: &str) -> HubGuard {
    check_test_hub(Some(url), &|_| None, None).expect("the test hub passes the strict guard")
}

/// Run a query future on a scoped worker thread, so the helper works inside an async test too.
fn block_on<T: Send>(future: impl Future<Output = T> + Send) -> T {
    std::thread::scope(|scope| {
        scope
            .spawn(move || {
                tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .expect("tokio runtime")
                    .block_on(future)
            })
            .join()
            .unwrap_or_else(|panic| std::panic::resume_unwind(panic))
    })
}

/// One boolean from a query on the hub, over a connection that lives for the query.
fn scalar_bool(url: &str, query: &str) -> bool {
    block_on(async {
        let (client, connection) = tokio_postgres::connect(url, NoTls)
            .await
            .expect("connect to the test hub");
        tokio::spawn(connection);
        client
            .query_one(query, &[])
            .await
            .expect("query the test hub")
            .get::<_, bool>(0)
    })
}

fn extension_installed(run_url: &str) -> bool {
    scalar_bool(
        run_url,
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_search')",
    )
}

fn session_open_on(admin: &str, name: &str) -> bool {
    scalar_bool(
        admin,
        &format!("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname = '{name}')"),
    )
}

/// Hold a session on the database with a long query; it ends when the database is dropped.
fn hold_session_open(run_url: String) {
    block_on(async move {
        let (client, connection) = tokio_postgres::connect(&run_url, NoTls)
            .await
            .expect("holder connects");
        tokio::spawn(connection);
        let _ = client.batch_execute("SELECT pg_sleep(60)").await;
    });
}

fn wait_until(mut condition: impl FnMut() -> bool, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    while !condition() {
        assert!(Instant::now() < deadline, "condition not reached in time");
        thread::sleep(Duration::from_millis(50));
    }
}

/// The pid of a child that has exited and been reaped, so it is dead.
fn dead_pid() -> u32 {
    // This test binary, asked for its help text, exits at once on every platform.
    let mut child = Command::new(std::env::current_exe().expect("this test binary"))
        .arg("--help")
        .spawn()
        .expect("spawn a child that exits");
    let pid = child.id();
    child.wait().expect("reap the child");
    pid
}

fn rehearsal_spec() -> serde_json::Value {
    serde_json::json!({
        "path": "absolute", "mode": "0600", "gobby_home": "<other>",
        "postgres_host": "127.0.0.1", "postgres_system_identifier": "7000000000000000001",
        "services_postgres_port": 61500, "content": "json",
    })
}

fn expect_guard_rule(error: DatabaseError, rule: &str) {
    match error {
        DatabaseError::Guard(refusal) => assert_eq!(refusal.rule, rule, "{refusal}"),
        other => panic!("expected a {rule} refusal, got: {other}"),
    }
}

#[test]
fn start_creates_the_run_database_named_for_this_process() {
    let Some(url) = test_hub_url() else {
        return;
    };
    let admin = maintenance_url(&url);
    let guard = strict_guard(&url);
    let run = start_run_database(&guard).expect("start a run");
    let name = run.name().expect("an owned run has a name").to_owned();
    assert_eq!(
        run_database_pid(&name, HARNESS_DATABASE_PREFIX),
        Some(std::process::id())
    );
    assert_eq!(
        run.url(),
        derive_run_url(&guard, &name, None).expect("derived url")
    );
    assert!(database_exists(&admin, &name).expect("existence query"));
    assert!(extension_installed(run.url()), "pg_search is installed");
    run.teardown().expect("teardown drops the run database");
    assert!(!database_exists(&admin, &name).expect("existence query"));
}

#[test]
fn teardown_drops_with_force_while_a_session_is_open() {
    let Some(url) = test_hub_url() else {
        return;
    };
    let admin = maintenance_url(&url);
    let guard = strict_guard(&url);
    let run = start_run_database(&guard).expect("start a run");
    let name = run.name().expect("an owned run has a name").to_owned();
    let holder = thread::spawn({
        let run_url = run.url().to_owned();
        move || hold_session_open(run_url)
    });
    wait_until(|| session_open_on(&admin, &name), Duration::from_secs(10));
    run.teardown()
        .expect("DROP ... WITH (FORCE) ends the open session");
    assert!(!database_exists(&admin, &name).expect("existence query"));
    holder.join().expect("the holder thread ends");
}

#[test]
fn sweep_drops_dead_pid_databases_and_keeps_live_ones() {
    let Some(url) = test_hub_url() else {
        return;
    };
    let admin = maintenance_url(&url);
    let dead_name = harness_database_name(dead_pid(), &new_run_suffix()).expect("dead name");
    let live_name =
        harness_database_name(std::process::id(), &new_run_suffix()).expect("live name");
    create_database(&admin, &dead_name).expect("plant the dead-pid database");
    create_database(&admin, &live_name).expect("plant the live-pid database");
    // Other test processes sweep the same hub, so this sweep may find the dead database already
    // gone; the postconditions hold either way.
    let swept = sweep_dead_pid(&admin).expect("sweep the hub");
    assert!(!swept.contains(&live_name), "swept a live pid: {swept:?}");
    assert!(!database_exists(&admin, &dead_name).expect("existence query"));
    assert!(database_exists(&admin, &live_name).expect("existence query"));
    drop_database(&admin, &live_name).expect("clean up the live-pid database");
}

#[test]
fn start_sweeps_a_dead_pid_database_before_creating_its_own() {
    let Some(url) = test_hub_url() else {
        return;
    };
    let admin = maintenance_url(&url);
    let dead_name = harness_database_name(dead_pid(), &new_run_suffix()).expect("dead name");
    create_database(&admin, &dead_name).expect("plant the dead-pid database");
    let run = start_run_database(&strict_guard(&url)).expect("start a run");
    assert!(!database_exists(&admin, &dead_name).expect("existence query"));
    run.teardown().expect("teardown");
}

/// A loopback port that is free and outside the shared ports, bound by this test so that any
/// connection the rehearsal path makes is visible as a pending accept.
fn rehearsal_listener() -> (TcpListener, u16) {
    loop {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind a loopback port");
        let port = listener.local_addr().expect("local address").port();
        if !SHARED_PORTS.contains(&port) {
            listener
                .set_nonblocking(true)
                .expect("nonblocking listener");
            return (listener, port);
        }
    }
}

#[test]
fn rehearsal_mode_creates_drops_and_sweeps_nothing() {
    // Rehearsal never touches the hub, so this test needs no hub. The profile's port is a
    // listener this test owns: create, sweep and drop all connect, and a pending accept on the
    // listener would show that a connection was attempted.
    let (listener, port) = rehearsal_listener();
    let rehearsal_url = format!("postgresql://gobby_test:gobby_test@127.0.0.1:{port}/gobby_test");
    let dir = tempfile::tempdir().expect("scratch dir");
    let mut spec = rehearsal_spec();
    spec["services_postgres_port"] = serde_json::json!(port);
    let profile = materialize_profile(&spec, dir.path());
    let guard = check_test_hub(Some(rehearsal_url.as_str()), &|_| None, profile.as_deref())
        .expect("the rehearsal profile is accepted");
    assert_eq!(guard.mode, GuardMode::Rehearsal);
    let run = start_run_database(&guard).expect("rehearsal start");
    assert_eq!(run.name(), None, "rehearsal creates no database");
    assert_eq!(run.url(), rehearsal_url, "rehearsal uses the profile's URL");
    run.teardown().expect("rehearsal teardown drops nothing");
    assert!(
        matches!(listener.accept(), Err(error) if error.kind() == ErrorKind::WouldBlock),
        "rehearsal opened a connection (create, sweep or drop)"
    );
}

#[test]
fn sweep_refuses_a_remote_endpoint_or_another_user_before_connecting() {
    let remote = sweep_dead_pid("postgresql://gobby_test:gobby_test@10.0.0.5:60892/postgres")
        .expect_err("a non-loopback endpoint");
    expect_guard_rule(remote, "R3");
    let live = sweep_dead_pid("postgresql://gobby:gobby@127.0.0.1:60891/postgres")
        .expect_err("the live hub's user");
    expect_guard_rule(live, "R3");
}

#[test]
fn create_refuses_names_outside_the_harness_pattern_before_connecting() {
    let refused = create_database(
        "postgresql://gobby_test:gobby_test@127.0.0.1:60892/postgres",
        "gobby",
    )
    .expect_err("a name outside the harness pattern");
    expect_guard_rule(refused, "ARG");
}

#[test]
fn a_run_dropped_while_unwinding_is_removed_without_aborting() {
    let Some(url) = test_hub_url() else {
        return;
    };
    let admin = maintenance_url(&url);
    let guard = strict_guard(&url);
    let recorded: Mutex<Option<String>> = Mutex::new(None);
    let outcome = std::panic::catch_unwind(AssertUnwindSafe(|| {
        let run = start_run_database(&guard).expect("start a run");
        *recorded.lock().expect("record the name") = run.name().map(str::to_owned);
        panic!("simulated test failure after the run started");
    }));
    assert!(outcome.is_err(), "the closure panicked");
    let name = recorded
        .into_inner()
        .expect("the name lock")
        .expect("the run was named before the panic");
    assert!(
        !database_exists(&admin, &name).expect("existence query"),
        "the drop during unwinding removed the run database"
    );
}

#[tokio::test(flavor = "current_thread")]
async fn start_teardown_and_drop_work_inside_a_tokio_runtime() {
    let Some(url) = test_hub_url() else {
        return;
    };
    let admin = maintenance_url(&url);
    let guard = strict_guard(&url);
    let run = start_run_database(&guard).expect("start a run inside a runtime");
    let name = run.name().expect("an owned run has a name").to_owned();
    assert!(extension_installed(run.url()), "pg_search is installed");
    run.teardown().expect("teardown inside a runtime");
    assert!(!database_exists(&admin, &name).expect("existence query"));

    let dropped = start_run_database(&guard).expect("start a second run");
    let dropped_name = dropped.name().expect("an owned run has a name").to_owned();
    drop(dropped);
    assert!(
        !database_exists(&admin, &dropped_name).expect("existence query"),
        "a run dropped inside a runtime is removed"
    );
}

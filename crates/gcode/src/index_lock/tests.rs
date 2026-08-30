//! Unit tests for [`crate::index_lock`].
//!
//! Kept out of the production file so its inline test module does not push
//! `index_lock.rs` over the 1,000-line ceiling (see `crates/AGENTS.md`).

use std::path::PathBuf;
use std::time::Duration;

use super::*;

fn context_for(database_url: String, project_id: &str) -> Context {
    Context {
        database_url,
        project_root: PathBuf::from("/tmp/gcode-index-lock-test"),
        project_id: project_id.to_string(),
        quiet: true,
        falkordb: None,
        qdrant: None,
        embedding: None,
        code_vectors: crate::config::CodeVectorSettings::default(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    }
}

fn connect_postgres_test_db() -> String {
    let database_url = crate::test_env::postgres_test_database_url("index-lock tests");
    db::connect_readwrite(&database_url).expect("connect index-lock PostgreSQL test database");
    database_url
}

fn hold_project_lock(database_url: &str, project_id: &str) -> Client {
    let mut conn = db::connect_readwrite(database_url).expect("connect test PostgreSQL hub");
    let key = project_lock_key(project_id);
    conn.execute("SELECT pg_advisory_lock($1)", &[&key])
        .expect("hold project advisory lock");
    conn
}

#[test]
fn project_lock_key_matches_fixture() {
    assert_eq!(project_lock_key("proj"), -9102099203869195108);
}

#[test]
fn project_lock_key_is_project_scoped() {
    assert_ne!(project_lock_key("proj-a"), project_lock_key("proj-b"));
}

#[test]
fn brief_index_flush_try_is_a_bounded_non_blocking_policy() {
    // The flush policy must be a bounded BriefTry (never Wait): a blocking
    // Wait waiter that is later killed pileup-wedges the index (#17701).
    match IndexLockPolicy::brief_index_flush_try() {
        IndexLockPolicy::BriefTry { total_wait, poll } => {
            assert!(total_wait > Duration::ZERO);
            assert!(poll > Duration::ZERO);
            assert!(poll < total_wait);
        }
        IndexLockPolicy::Wait { .. } => {
            panic!("flush policy must not be the blocking Wait policy")
        }
    }
}

/// Records every `log` line so a test can prove which SQL a lock path sent:
/// the postgres driver logs each prepared statement at debug level.
struct RecordingLogger {
    records: std::sync::Mutex<Vec<String>>,
}

impl RecordingLogger {
    fn lock_records(&self) -> std::sync::MutexGuard<'_, Vec<String>> {
        self.records
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl log::Log for RecordingLogger {
    fn enabled(&self, _metadata: &log::Metadata<'_>) -> bool {
        true
    }

    fn log(&self, record: &log::Record<'_>) {
        self.lock_records()
            .push(format!("{}: {}", record.level(), record.args()));
    }

    fn flush(&self) {}
}

static RECORDING_LOGGER: RecordingLogger = RecordingLogger {
    records: std::sync::Mutex::new(Vec::new()),
};
static RECORDING_LOGGER_INIT: std::sync::Once = std::sync::Once::new();

fn capture_logs<R>(f: impl FnOnce() -> R) -> (R, Vec<String>) {
    RECORDING_LOGGER_INIT.call_once(|| {
        log::set_logger(&RECORDING_LOGGER).expect("install recording logger");
        log::set_max_level(log::LevelFilter::Debug);
    });
    RECORDING_LOGGER.lock_records().clear();
    let result = f();
    let records = RECORDING_LOGGER.lock_records().clone();
    (result, records)
}

fn unlock_lines(records: &[String]) -> Vec<&String> {
    records
        .iter()
        .filter(|line| {
            line.contains("pg_advisory_unlock") || line.contains("was not held during unlock")
        })
        .collect()
}

mod serial_db {
    use super::*;

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn busy_project_id_lock_sends_no_unlock_while_an_acquired_guard_does() {
        let database_url = connect_postgres_test_db();
        let project_id = "gcode-lock-by-id-busy-no-unlock";
        let holder = hold_project_lock(&database_url, project_id);

        let (busy, busy_logs) = capture_logs(|| {
            lock_project_by_id(
                &database_url,
                project_id,
                IndexLockPolicy::maintenance_try(),
                LockDiagnostics::Silent,
            )
        });

        assert!(
            matches!(
                busy.expect("busy project lock must defer without error"),
                IndexLockResult::Busy(_)
            ),
            "busy project lock must report contention"
        );
        let leaked = unlock_lines(&busy_logs);
        assert!(
            leaked.is_empty(),
            "a busy try-lock must not release a lock it never acquired: {leaked:?}"
        );

        drop(holder);

        let (_, acquired_logs) = capture_logs(|| {
            let IndexLockResult::Acquired(guard) = lock_project_by_id(
                &database_url,
                project_id,
                IndexLockPolicy::maintenance_try(),
                LockDiagnostics::Silent,
            )
            .expect("acquire project lock by id") else {
                panic!("project lock should be available once the holder releases it");
            };
            drop(guard);
        });

        assert!(
            acquired_logs
                .iter()
                .any(|line| line.contains("pg_advisory_unlock")),
            "an acquired guard must release its lock on drop: {acquired_logs:?}"
        );
        assert!(
            !acquired_logs
                .iter()
                .any(|line| line.contains("was not held during unlock")),
            "an acquired guard must own the lock it releases: {acquired_logs:?}"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn project_id_lock_acquires_and_releases_guard() {
        let database_url = connect_postgres_test_db();
        let project_id = "gcode-lock-by-id-acquire";

        let IndexLockResult::Acquired(guard) = lock_project_by_id(
            &database_url,
            project_id,
            IndexLockPolicy::maintenance_try(),
            LockDiagnostics::Silent,
        )
        .expect("acquire project lock by id") else {
            panic!("project lock should be available");
        };

        assert!(
            matches!(
                lock_project_by_id(
                    &database_url,
                    project_id,
                    IndexLockPolicy::maintenance_try(),
                    LockDiagnostics::Silent,
                )
                .expect("retry project lock by id"),
                IndexLockResult::Busy(_)
            ),
            "guard must hold the project advisory lock"
        );

        drop(guard);

        assert!(
            matches!(
                lock_project_by_id(
                    &database_url,
                    project_id,
                    IndexLockPolicy::maintenance_try(),
                    LockDiagnostics::Silent,
                )
                .expect("reacquire released project lock by id"),
                IndexLockResult::Acquired(_)
            ),
            "dropping the guard must release the project advisory lock"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn leased_lock_on_a_borrowed_connection_releases_on_drop() {
        let database_url = connect_postgres_test_db();
        let project_id = "gcode-lock-lease";
        let mut conn = db::connect_readwrite(&database_url).expect("connect lease connection");

        let lease = lease_project_lock(&mut conn, project_id, IndexLockPolicy::maintenance_try())
            .expect("lease project lock")
            .expect("project lock should be available");
        assert!(
            matches!(
                lock_project_by_id(
                    &database_url,
                    project_id,
                    IndexLockPolicy::maintenance_try(),
                    LockDiagnostics::Silent,
                )
                .expect("retry project lock from another session"),
                IndexLockResult::Busy(_)
            ),
            "the lease must hold the project advisory lock"
        );

        drop(lease);

        assert!(
            lease_project_lock(&mut conn, project_id, IndexLockPolicy::maintenance_try())
                .expect("re-lease on the same connection")
                .is_some(),
            "dropping the lease must release the lock without closing the connection"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn maintenance_project_id_lock_defers_when_busy() {
        let database_url = connect_postgres_test_db();
        let project_id = "gcode-lock-by-id-busy";
        let _holder = hold_project_lock(&database_url, project_id);
        let started = Instant::now();

        let result = lock_project_by_id(
            &database_url,
            project_id,
            IndexLockPolicy::maintenance_try(),
            LockDiagnostics::Silent,
        )
        .expect("busy project lock must defer without error");

        assert!(
            matches!(result, IndexLockResult::Busy(_)),
            "busy project lock must report contention"
        );
        assert!(
            started.elapsed() >= Duration::from_millis(150),
            "maintenance lock must try for its full bounded wait"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn brief_try_returns_busy_while_same_project_lock_is_held() {
        let database_url = connect_postgres_test_db();
        let ctx = context_for(database_url.clone(), "gcode-lock-brief-try");
        let _holder = hold_project_lock(&database_url, &ctx.project_id);

        let result = with_project_lock::<()>(
            &ctx,
            IndexLockPolicy::BriefTry {
                total_wait: Duration::from_millis(50),
                poll: Duration::from_millis(10),
            },
            || anyhow::bail!("closure must not run while lock is busy"),
        )
        .expect("try project lock");

        assert_eq!(
            result,
            IndexLockResult::Busy(None),
            "a quiet context must not pay for the holder lookup"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn wait_blocks_until_same_project_lock_is_released() {
        let database_url = connect_postgres_test_db();
        let project_id = "gcode-lock-wait";
        let ctx = context_for(database_url.clone(), project_id);
        let holder = hold_project_lock(&database_url, project_id);

        let (done_tx, done_rx) = std::sync::mpsc::channel();
        let handle = std::thread::spawn(move || {
            let result =
                with_project_lock(&ctx, IndexLockPolicy::wait(), || Ok::<_, anyhow::Error>(()));
            done_tx.send(()).expect("send wait lock completion");
            result
        });
        assert!(
            done_rx.recv_timeout(Duration::from_millis(100)).is_err(),
            "wait policy did not block"
        );

        drop(holder);
        let result = handle
            .join()
            .expect("wait lock thread joins")
            .expect("wait lock succeeds");
        assert_eq!(result, IndexLockResult::Acquired(()));
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn different_project_ids_do_not_block_each_other() {
        let database_url = connect_postgres_test_db();
        let _holder = hold_project_lock(&database_url, "gcode-lock-held-project");
        let ctx = context_for(database_url, "gcode-lock-free-project");

        let result = with_project_lock(
            &ctx,
            IndexLockPolicy::BriefTry {
                total_wait: Duration::from_millis(10),
                poll: Duration::from_millis(1),
            },
            || Ok::<_, anyhow::Error>(7),
        )
        .expect("try different project lock");

        assert_eq!(result, IndexLockResult::Acquired(7));
    }

    /// Regression guard for #17482: `commands::index::run` runs the
    /// graph/vector projection sync INSIDE the `with_project_lock` closure,
    /// so the per-project advisory lock is held for the entire index+sync run.
    /// This proves that once execution is inside the locked closure — past the
    /// index phase, in the projection-sync phase — a competing acquirer of the
    /// same `project_lock_key` on another connection is still refused (Busy).
    /// If the sync were moved back outside the lock, the projection phase would
    /// no longer be covered; this test pins the lock over the full closure body.
    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn projection_sync_phase_is_covered_by_the_project_lock() {
        let database_url = connect_postgres_test_db();
        let project_id = "gcode-lock-projection-phase";
        let ctx = context_for(database_url.clone(), project_id);
        let key = project_lock_key(project_id);

        // Model run()'s locked body: the index phase has completed and the
        // projection-sync phase is now running — still inside the closure.
        // From there, a second acquirer must observe the lock as held.
        let observed = with_project_lock(&ctx, IndexLockPolicy::wait(), || {
            let mut competitor =
                db::connect_readwrite(&database_url).expect("connect competing acquirer");
            let acquired = try_advisory_lock(&mut competitor, key)
                .expect("probe project lock during projection-sync phase");
            if acquired {
                // Never expected; release so a violated guarantee doesn't leak.
                competitor
                    .execute("SELECT pg_advisory_unlock($1)", &[&key])
                    .expect("release probe lock");
            }
            Ok::<_, anyhow::Error>(!acquired)
        })
        .expect("acquire project lock");

        assert_eq!(
            observed,
            IndexLockResult::Acquired(true),
            "project lock must remain held during the projection-sync phase"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn wait_policy_errors_after_cap_when_holder_never_releases() {
        // A hung holder must surface as a loud error, not an indefinite
        // hang: the bounded Wait acquisition gives up after its cap and
        // returns Err (never a silent Busy), so a stalled index or codewiki
        // run fails visibly instead of parking forever in pg_advisory_lock
        // (#17709).
        let database_url = connect_postgres_test_db();
        let project_id = "gcode-lock-wait-cap";
        let ctx = context_for(database_url.clone(), project_id);
        let mut holder = hold_project_lock(&database_url, project_id);
        let holder_pid: i32 = holder
            .query_one("SELECT pg_backend_pid()", &[])
            .expect("read holder backend pid")
            .get(0);

        let started = Instant::now();
        let result = with_project_lock(
            &ctx,
            IndexLockPolicy::Wait {
                max_wait: Duration::from_millis(300),
            },
            || Ok::<_, anyhow::Error>(()),
        );

        let error = format!(
            "{:#}",
            result.expect_err("bounded Wait must error when the holder never releases")
        );
        assert!(
            started.elapsed() < Duration::from_secs(30),
            "bounded Wait must give up near its cap, not hang"
        );
        // Giving up is an error, so it names the holder even though this
        // context is quiet: an operator who only learns "busy" has to guess
        // which backend to terminate.
        assert!(
            error.contains(&format!("backend pid {holder_pid}")),
            "the give-up error must name the holding backend: {error}"
        );
        assert!(
            error.contains("connection age"),
            "the give-up error must say how long that backend has been around: {error}"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn busy_result_names_the_backend_holding_the_project_lock() {
        let database_url = connect_postgres_test_db();
        let project_id = "gcode-lock-busy-holder";
        let ctx = Context {
            quiet: false,
            ..context_for(database_url.clone(), project_id)
        };
        let mut holder = hold_project_lock(&database_url, project_id);
        let holder_pid: i32 = holder
            .query_one("SELECT pg_backend_pid()", &[])
            .expect("read holder backend pid")
            .get(0);

        let result = with_project_lock::<()>(
            &ctx,
            IndexLockPolicy::BriefTry {
                total_wait: Duration::from_millis(50),
                poll: Duration::from_millis(10),
            },
            || anyhow::bail!("closure must not run while lock is busy"),
        )
        .expect("try project lock");

        let IndexLockResult::Busy(Some(description)) = result else {
            panic!("a contended acquisition must identify its holder, got {result:?}");
        };
        assert!(
            description.contains(&format!("backend pid {holder_pid}")),
            "holder description must name the holding backend: {description}"
        );
        // `gobby-cli` is what every gcode connection registers as; the point
        // is that the field is reported, not that it is unique.
        assert!(
            description.contains("application_name"),
            "holder description must report application_name: {description}"
        );
        assert!(
            description.contains("state"),
            "holder description must report backend state: {description}"
        );
        assert!(
            description.contains("connection age"),
            "holder description must report how long the holder has been around: {description}"
        );
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn a_blocked_wait_names_the_holder_repeatedly_instead_of_going_silent() {
        let database_url = connect_postgres_test_db();
        let project_id = "gcode-lock-wait-notice";
        let mut holder = hold_project_lock(&database_url, project_id);
        let holder_pid: i32 = holder
            .query_one("SELECT pg_backend_pid()", &[])
            .expect("read holder backend pid")
            .get(0);
        let key = project_lock_key(project_id);
        let mut waiter = db::connect_readwrite(&database_url).expect("connect blocked acquirer");

        let (acquired, records) = capture_logs(|| {
            try_advisory_lock_until(
                &mut waiter,
                key,
                Duration::from_millis(300),
                Duration::from_millis(20),
                Some(Duration::from_millis(50)),
            )
        });

        assert!(
            !acquired.expect("polling a held lock must not error"),
            "the holder never released, so the wait must end unacquired"
        );
        let notices: Vec<&String> = records
            .iter()
            .filter(|line| line.contains("waiting for the gcode index lock"))
            .collect();
        // One line at the threshold and then silence for the rest of a
        // 30-minute cap is the defect; the notice has to keep reporting.
        assert!(
            notices.len() >= 2,
            "a blocked wait must keep naming its holder, got {notices:?}"
        );
        assert!(
            notices
                .iter()
                .all(|line| line.contains(&format!("backend pid {holder_pid}"))),
            "every notice must name the holding backend: {notices:?}"
        );
        assert!(
            notices[0].contains("giving up after 0s"),
            "the notice must state the deadline it is counting down to: {notices:?}"
        );
    }
}

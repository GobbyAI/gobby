use std::time::{Duration, Instant};

use anyhow::Context as _;
use postgres::Client;
use sha2::{Digest, Sha256};

use crate::config::Context;
use crate::db;

const MIN_LOCK_POLL: Duration = Duration::from_millis(1);
const ADVISORY_LOCK_DELAY_WARNING_MS_ENV: &str = "GCODE_ADVISORY_LOCK_DELAY_WARNING_MS";
const DEFAULT_ADVISORY_LOCK_DELAY_WARNING_MS: u64 = 30_000;

/// Upper bound for the blocking [`IndexLockPolicy::Wait`] acquisition. Normal
/// index/codewiki lock handoffs complete in seconds; only a genuinely hung
/// holder (e.g. a projection sync wedged on FalkorDB or Qdrant) reaches this
/// cap. Exceeding it fails loudly rather than parking indefinitely in
/// `pg_advisory_lock`, which — exactly like the flush path in #17701 — would
/// otherwise leave an un-reclaimable waiter behind if the process is killed.
const DEFAULT_WAIT_LOCK_TIMEOUT: Duration = Duration::from_secs(1800);
/// Poll cadence for the bounded [`IndexLockPolicy::Wait`] acquisition.
const WAIT_LOCK_POLL: Duration = Duration::from_millis(250);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum IndexLockPolicy {
    Wait {
        max_wait: Duration,
    },
    BriefTry {
        total_wait: Duration,
        poll: Duration,
    },
}

impl IndexLockPolicy {
    pub(crate) fn brief_freshness_try() -> Self {
        Self::maintenance_try()
    }

    pub(crate) fn maintenance_try() -> Self {
        Self::BriefTry {
            total_wait: Duration::from_millis(150),
            poll: Duration::from_millis(25),
        }
    }

    /// Skip-if-busy policy for daemon-triggered per-file index flushes.
    ///
    /// A flush that observes a held index lock (typically a full reindex, which
    /// re-indexes the same files anyway) must yield rather than block on
    /// `pg_advisory_lock`: a blocking waiter that is later SIGKILLed by the
    /// flush timeout leaves a lingering advisory-lock waiter (Postgres does not
    /// notice the client disconnect while a backend is parked in the lock wait),
    /// which is how a single stuck reindex produced a 40+ deep waiter pileup
    /// (#17701). `BriefTry` polls `pg_try_advisory_lock` instead, so a killed
    /// flush is idle between polls and its backend is reclaimed promptly.
    pub(crate) fn brief_index_flush_try() -> Self {
        Self::BriefTry {
            total_wait: Duration::from_secs(3),
            poll: Duration::from_millis(200),
        }
    }

    /// Blocking-until-available policy with a generous safety cap, for the full
    /// index, `init`, and codewiki runs. Poll-based (never a raw blocking
    /// `pg_advisory_lock`) so a killed waiter is reclaimed promptly, and bounded
    /// so a hung holder surfaces as a loud error instead of an indefinite hang.
    pub(crate) fn wait() -> Self {
        Self::Wait {
            max_wait: DEFAULT_WAIT_LOCK_TIMEOUT,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum IndexLockResult<T> {
    Acquired(T),
    Busy,
}

pub(crate) fn with_project_lock<T>(
    ctx: &Context,
    policy: IndexLockPolicy,
    f: impl FnOnce() -> anyhow::Result<T>,
) -> anyhow::Result<IndexLockResult<T>> {
    match acquire_project_lock(ctx, policy)? {
        ProjectIndexLockAttempt::Acquired(_guard) => f().map(IndexLockResult::Acquired),
        ProjectIndexLockAttempt::Busy => Ok(IndexLockResult::Busy),
    }
}

enum ProjectIndexLockAttempt {
    Acquired(Box<ProjectIndexLock>),
    Busy,
}

pub(crate) fn lock_project_by_id(
    database_url: &str,
    project_id: &str,
    policy: IndexLockPolicy,
) -> anyhow::Result<Option<ProjectIndexLock>> {
    let key = project_lock_key(project_id);
    let mut conn = db::connect_readwrite(database_url)
        .with_context(|| "failed to connect PostgreSQL hub for gcode index lock")?;

    let acquired = match policy {
        IndexLockPolicy::Wait { max_wait } => {
            // Poll rather than block in `pg_advisory_lock`: a parked blocking
            // waiter is not reclaimed when its client dies (#17701), and an
            // unbounded wait lets one hung holder starve every other index and
            // codewiki run indefinitely. Bounded + poll-based means a killed
            // waiter is idle between polls, and a genuinely hung holder trips
            // the cap and fails loudly below instead of hanging forever.
            if !try_advisory_lock_until(&mut conn, key, max_wait, WAIT_LOCK_POLL)? {
                anyhow::bail!(
                    "gave up acquiring gcode index lock for project {} after {}s: \
                     a lock holder is likely hung (check for a stalled index or \
                     codewiki run)",
                    project_id,
                    max_wait.as_secs(),
                );
            }
            true
        }
        IndexLockPolicy::BriefTry { total_wait, poll } => {
            try_advisory_lock_until(&mut conn, key, total_wait, poll)?
        }
    };

    // `then` builds the guard only once the lock is held. An eagerly built
    // guard was dropped on the busy path, and its `Drop` sent
    // `pg_advisory_unlock` for a lock this session never owned (#21053).
    Ok(acquired.then(|| ProjectIndexLock {
        conn,
        key,
        quiet: true,
    }))
}

fn acquire_project_lock(
    ctx: &Context,
    policy: IndexLockPolicy,
) -> anyhow::Result<ProjectIndexLockAttempt> {
    let started = Instant::now();

    match lock_project_by_id(&ctx.database_url, &ctx.project_id, policy)? {
        Some(mut guard) => {
            guard.quiet = ctx.quiet;
            let elapsed = started.elapsed();
            if !ctx.quiet && elapsed >= advisory_lock_delay_warning() {
                eprintln!(
                    "warning: waited {}ms to acquire gcode index lock",
                    elapsed.as_millis()
                );
            }
            Ok(ProjectIndexLockAttempt::Acquired(Box::new(guard)))
        }
        None => Ok(ProjectIndexLockAttempt::Busy),
    }
}

fn try_advisory_lock_until(
    conn: &mut Client,
    key: i64,
    total_wait: Duration,
    poll: Duration,
) -> anyhow::Result<bool> {
    let started = Instant::now();
    loop {
        if try_advisory_lock(conn, key)? {
            return Ok(true);
        }

        let elapsed = started.elapsed();
        if elapsed >= total_wait {
            return Ok(false);
        }

        let remaining = total_wait - elapsed;
        let sleep_for = if poll.is_zero() {
            Duration::ZERO
        } else {
            poll.max(MIN_LOCK_POLL).min(remaining)
        };
        if sleep_for.is_zero() {
            // A zero poll interval intentionally means aggressive retry with a
            // scheduler yield only; callers use it only for very short windows.
            std::thread::yield_now();
        } else {
            std::thread::sleep(sleep_for);
        }
    }
}

fn try_advisory_lock(conn: &mut Client, key: i64) -> anyhow::Result<bool> {
    let row = conn
        .query_one("SELECT pg_try_advisory_lock($1)", &[&key])
        .with_context(|| "failed to try gcode index lock")?;
    row.try_get(0).map_err(Into::into)
}

pub(crate) fn project_lock_key(project_id: &str) -> i64 {
    // PostgreSQL advisory locks are 64-bit; this truncates SHA-256 and accepts
    // the residual collision risk in exchange for deterministic project locks.
    let mut hasher = Sha256::new();
    hasher.update(b"gcode:index:");
    hasher.update(project_id.as_bytes());
    let digest = hasher.finalize();
    i64::from_be_bytes(
        digest[0..8]
            .try_into()
            .expect("SHA-256 digest has at least 8 bytes"),
    )
}

fn advisory_lock_delay_warning() -> Duration {
    std::env::var(ADVISORY_LOCK_DELAY_WARNING_MS_ENV)
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .map(Duration::from_millis)
        .unwrap_or_else(|| Duration::from_millis(DEFAULT_ADVISORY_LOCK_DELAY_WARNING_MS))
}

pub(crate) struct ProjectIndexLock {
    conn: Client,
    key: i64,
    quiet: bool,
}

impl Drop for ProjectIndexLock {
    fn drop(&mut self) {
        let result = self
            .conn
            .query_one("SELECT pg_advisory_unlock($1)", &[&self.key]);
        match result {
            Ok(row) => match row.try_get::<_, bool>(0) {
                Ok(true) => {}
                Ok(false) => {
                    log::debug!("gcode index lock was not held during unlock");
                    if !self.quiet {
                        eprintln!("warning: gcode index lock was not held during unlock");
                    }
                }
                Err(error) => {
                    log::debug!("failed to read gcode index unlock result: {error}");
                    if !self.quiet {
                        eprintln!("warning: failed to read gcode index unlock result: {error}");
                    }
                }
            },
            Err(error) => {
                log::debug!("failed to release gcode index lock: {error}");
                if !self.quiet {
                    eprintln!("warning: failed to release gcode index lock: {error}");
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
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
                )
            });

            assert!(
                busy.expect("busy project lock must defer without error")
                    .is_none(),
                "busy project lock must return None"
            );
            let leaked = unlock_lines(&busy_logs);
            assert!(
                leaked.is_empty(),
                "a busy try-lock must not release a lock it never acquired: {leaked:?}"
            );

            drop(holder);

            let (_, acquired_logs) = capture_logs(|| {
                let guard = lock_project_by_id(
                    &database_url,
                    project_id,
                    IndexLockPolicy::maintenance_try(),
                )
                .expect("acquire project lock by id")
                .expect("project lock should be available once the holder releases it");
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

            let guard = lock_project_by_id(
                &database_url,
                project_id,
                IndexLockPolicy::maintenance_try(),
            )
            .expect("acquire project lock by id")
            .expect("project lock should be available");

            assert!(
                lock_project_by_id(
                    &database_url,
                    project_id,
                    IndexLockPolicy::maintenance_try(),
                )
                .expect("retry project lock by id")
                .is_none(),
                "guard must hold the project advisory lock"
            );

            drop(guard);

            assert!(
                lock_project_by_id(
                    &database_url,
                    project_id,
                    IndexLockPolicy::maintenance_try(),
                )
                .expect("reacquire released project lock by id")
                .is_some(),
                "dropping the guard must release the project advisory lock"
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
            )
            .expect("busy project lock must defer without error");

            assert!(result.is_none(), "busy project lock must return None");
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

            assert_eq!(result, IndexLockResult::Busy);
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
            let _holder = hold_project_lock(&database_url, project_id);

            let started = Instant::now();
            let result = with_project_lock(
                &ctx,
                IndexLockPolicy::Wait {
                    max_wait: Duration::from_millis(300),
                },
                || Ok::<_, anyhow::Error>(()),
            );

            assert!(
                result.is_err(),
                "bounded Wait must error when the holder never releases, got {result:?}"
            );
            assert!(
                started.elapsed() < Duration::from_secs(30),
                "bounded Wait must give up near its cap, not hang"
            );
        }
    }
}

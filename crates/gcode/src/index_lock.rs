use std::time::{Duration, Instant};

use anyhow::Context as _;
use postgres::Client;
use sha2::{Digest, Sha256};

use crate::config::Context;
use crate::db;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum IndexLockPolicy {
    Wait,
    BriefTry {
        total_wait: Duration,
        poll: Duration,
    },
}

impl IndexLockPolicy {
    pub(crate) fn brief_freshness_try() -> Self {
        Self::BriefTry {
            total_wait: Duration::from_millis(150),
            poll: Duration::from_millis(25),
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

fn acquire_project_lock(
    ctx: &Context,
    policy: IndexLockPolicy,
) -> anyhow::Result<ProjectIndexLockAttempt> {
    let key = project_lock_key(&ctx.project_id);
    let mut conn = db::connect_readwrite(&ctx.database_url)
        .with_context(|| "failed to connect PostgreSQL hub for gcode index lock")?;

    let acquired = match policy {
        IndexLockPolicy::Wait => {
            conn.execute("SELECT pg_advisory_lock($1)", &[&key])
                .with_context(|| "failed to acquire gcode index lock")?;
            true
        }
        IndexLockPolicy::BriefTry { total_wait, poll } => {
            try_advisory_lock_until(&mut conn, key, total_wait, poll)?
        }
    };

    if acquired {
        Ok(ProjectIndexLockAttempt::Acquired(Box::new(
            ProjectIndexLock {
                conn,
                key,
                quiet: ctx.quiet,
            },
        )))
    } else {
        Ok(ProjectIndexLockAttempt::Busy)
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
        let sleep_for = poll.min(remaining);
        if sleep_for.is_zero() {
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

struct ProjectIndexLock {
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
                Ok(false) if !self.quiet => {
                    eprintln!("warning: gcode index lock was not held during unlock");
                }
                Ok(false) => {}
                Err(error) if !self.quiet => {
                    eprintln!("warning: failed to read gcode index unlock result: {error}");
                }
                Err(_) => {}
            },
            Err(error) if !self.quiet => {
                eprintln!("warning: failed to release gcode index lock: {error}");
            }
            Err(_) => {}
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
            daemon_url: None,
            index_scope: crate::config::ProjectIndexScope::Single,
        }
    }

    fn connect_postgres_test_db() -> Option<String> {
        let database_url = std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL").ok()?;
        match db::connect_readwrite(&database_url) {
            Ok(_) => Some(database_url),
            Err(error) => {
                eprintln!("skipping advisory-lock test: PostgreSQL hub is unavailable: {error}");
                None
            }
        }
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
    #[serial_test::serial]
    fn brief_try_returns_busy_while_same_project_lock_is_held() {
        let Some(database_url) = connect_postgres_test_db() else {
            return;
        };
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
    #[serial_test::serial]
    fn wait_blocks_until_same_project_lock_is_released() {
        let Some(database_url) = connect_postgres_test_db() else {
            return;
        };
        let project_id = "gcode-lock-wait";
        let ctx = context_for(database_url.clone(), project_id);
        let holder = hold_project_lock(&database_url, project_id);

        let (done_tx, done_rx) = std::sync::mpsc::channel();
        let handle = std::thread::spawn(move || {
            let result =
                with_project_lock(&ctx, IndexLockPolicy::Wait, || Ok::<_, anyhow::Error>(()));
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
    #[serial_test::serial]
    fn different_project_ids_do_not_block_each_other() {
        let Some(database_url) = connect_postgres_test_db() else {
            return;
        };
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
}

use super::*;
use anyhow::Context;
use std::collections::HashSet;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;
use tokio::sync::{Notify, oneshot};
use tokio_postgres::NoTls;
use tokio_postgres::config::Host;
use tokio_postgres::error::SqlState;
use tokio_postgres::types::ToSql;

/// The isolated test hub, or `None` (skip) when the variable is unset. A URL
/// naming anything but a `*_test` database is refused rather than used.
fn test_database_url() -> Option<String> {
    let Ok(database_url) = std::env::var("GOBBY_SCHEMA_TEST_DATABASE_URL") else {
        eprintln!("GOBBY_SCHEMA_TEST_DATABASE_URL is unset; skipping PostgreSQL pool test");
        return None;
    };
    let config: tokio_postgres::Config = database_url
        .parse()
        .expect("GOBBY_SCHEMA_TEST_DATABASE_URL must be a PostgreSQL URL");
    let database = config.get_dbname().unwrap_or_default();
    assert!(
        database.ends_with("_test"),
        "GOBBY_SCHEMA_TEST_DATABASE_URL must name an isolated *_test database, not `{database}`"
    );
    Some(database_url)
}

/// A fresh `gobby-gdaemon` name, so concurrent tests never count each other's backends.
fn unique_application_name() -> String {
    format!("gobby-gdaemon-test-{}", uuid::Uuid::new_v4().simple())
}

fn settings(application_name: &str, max_size: usize, acquire_timeout: Duration) -> PoolSettings {
    PoolSettings {
        max_size,
        application_name: application_name.to_string(),
        acquire_timeout,
    }
}

/// Backend PID, role, time zone, and application name of one connection.
async fn session(client: &tokio_postgres::Client) -> anyhow::Result<(i32, String, String, String)> {
    let row = client
        .query_one(
            "SELECT pg_backend_pid(), current_user::text, current_setting('TimeZone'), \
             current_setting('application_name')",
            &[],
        )
        .await?;
    Ok((
        row.try_get(0)?,
        row.try_get(1)?,
        row.try_get(2)?,
        row.try_get(3)?,
    ))
}

/// An unpooled connection for observing the server or administering scratch objects.
async fn direct_connection(database_url: &str) -> anyhow::Result<tokio_postgres::Client> {
    let (client, connection) = tokio_postgres::connect(database_url, NoTls).await?;
    tokio::spawn(connection);
    Ok(client)
}

async fn server_connections(
    observer: &tokio_postgres::Client,
    application_name: &str,
) -> anyhow::Result<i64> {
    let row = observer
        .query_one(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = $1",
            &[&application_name],
        )
        .await?;
    Ok(row.try_get(0)?)
}

fn error_chain(error: &(dyn std::error::Error + 'static)) -> String {
    let mut text = error.to_string();
    let mut source = error.source();
    while let Some(cause) = source {
        text.push_str(": ");
        text.push_str(&cause.to_string());
        source = cause.source();
    }
    text
}

#[tokio::test]
async fn checkout_runs_as_runtime_role() -> anyhow::Result<()> {
    for refused in ["gobby-cli", "gdaemon-gobby", "gobby-gdaemon-\u{e9}"] {
        let outcome = Pool::build(
            "postgresql://gobby@127.0.0.1/gobby_test",
            settings(refused, 2, Duration::from_secs(1)),
        );
        assert!(
            matches!(outcome, Err(PoolError::InvalidApplicationName(_))),
            "application_name `{refused}` must be refused"
        );
    }

    let Some(database_url) = test_database_url() else {
        return Ok(());
    };
    let name = unique_application_name();
    let pool = Pool::build(&database_url, settings(&name, 2, Duration::from_secs(5)))?;
    let expected = |pid| {
        (
            pid,
            "gobby_daemon_runtime".to_string(),
            "UTC".to_string(),
            name.clone(),
        )
    };

    let created = pool.get().await?;
    let (pid, ..) = session(&created).await?;
    assert_eq!(session(&created).await?, expected(pid), "new connection");
    drop(created);

    let recycled = pool.get().await?;
    assert_eq!(
        session(&recycled).await?,
        expected(pid),
        "recycled connection"
    );
    Ok(())
}

#[tokio::test]
async fn recycle_restores_or_discards_session_state() -> anyhow::Result<()> {
    let Some(database_url) = test_database_url() else {
        return Ok(());
    };
    let name = unique_application_name();
    let pool = Pool::build(&database_url, settings(&name, 2, Duration::from_secs(5)))?;
    let _held = pool.get().await?;

    let borrower = pool.get().await?;
    let (pid, ..) = session(&borrower).await?;
    borrower
        .batch_execute("SET TIME ZONE 'America/Chicago'; SET application_name = 'unrelated'")
        .await?;
    drop(borrower);

    let reused = pool.get().await?;
    assert_eq!(
        session(&reused).await?,
        (
            pid,
            "gobby_daemon_runtime".to_string(),
            "UTC".to_string(),
            name.clone()
        ),
        "time zone and application name drift is restored on the same backend"
    );
    reused.batch_execute("RESET ROLE").await?;
    drop(reused);

    let replacement = pool.get().await?;
    let (replacement_pid, role, ..) = session(&replacement).await?;
    assert_ne!(
        replacement_pid, pid,
        "a backend that left the runtime role is discarded"
    );
    assert_eq!(role, "gobby_daemon_runtime");
    Ok(())
}

#[tokio::test]
async fn dropped_transaction_is_rolled_back() -> anyhow::Result<()> {
    let Some(database_url) = test_database_url() else {
        return Ok(());
    };
    let name = unique_application_name();
    let pool = Pool::build(&database_url, settings(&name, 2, Duration::from_secs(5)))?;
    let _held = pool.get().await?;

    let mut borrower = pool.get().await?;
    let (pid, ..) = session(&borrower).await?;
    let transaction = borrower.transaction().await?;
    transaction
        .batch_execute(
            "CREATE TEMP TABLE pool_dropped_writes (value int); \
             INSERT INTO pool_dropped_writes VALUES (1)",
        )
        .await?;
    drop(transaction);
    drop(borrower);

    let reused = pool.get().await?;
    let row = reused
        .query_one(
            "SELECT pg_backend_pid(), pg_current_xact_id_if_assigned() IS NULL, \
             to_regclass('pg_temp.pool_dropped_writes') IS NULL",
            &[],
        )
        .await?;
    assert_eq!(
        row.try_get::<_, i32>(0)?,
        pid,
        "the rolled-back backend is reused"
    );
    assert!(row.try_get::<_, bool>(1)?, "no transaction is left open");
    assert!(row.try_get::<_, bool>(2)?, "the dropped writes are gone");
    Ok(())
}

#[tokio::test]
async fn raw_transaction_never_reaches_the_next_borrower() -> anyhow::Result<()> {
    let Some(database_url) = test_database_url() else {
        return Ok(());
    };
    let name = unique_application_name();
    let pool = Pool::build(&database_url, settings(&name, 2, Duration::from_secs(5)))?;
    let _held = pool.get().await?;

    let borrower = pool.get().await?;
    let (pid, ..) = session(&borrower).await?;
    borrower
        .batch_execute(
            "BEGIN; CREATE TEMP TABLE pool_raw_writes (value int); \
             INSERT INTO pool_raw_writes VALUES (1)",
        )
        .await?;
    drop(borrower);

    let reused = pool.get().await?;
    let row = reused
        .query_one(
            "SELECT pg_backend_pid(), pg_current_xact_id_if_assigned() IS NULL, \
             to_regclass('pg_temp.pool_raw_writes') IS NULL",
            &[],
        )
        .await?;
    assert_eq!(
        row.try_get::<_, i32>(0)?,
        pid,
        "the backend is reused once its transaction ends"
    );
    assert!(row.try_get::<_, bool>(1)?, "no transaction is left open");
    assert!(row.try_get::<_, bool>(2)?, "the uncommitted write is gone");

    if reused.batch_execute("BEGIN; SELECT 1 / 0").await.is_ok() {
        anyhow::bail!("division by zero must abort the transaction");
    }
    drop(reused);

    // A statement on an aborted transaction fails, so this query succeeding
    // proves the next borrower is outside it.
    let next = pool.get().await?;
    let idle: bool = next
        .query_one("SELECT pg_current_xact_id_if_assigned() IS NULL", &[])
        .await?
        .try_get(0)?;
    assert!(idle, "no transaction is left open after an abort");
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn pool_bounds_server_connections() -> anyhow::Result<()> {
    const POOL_SIZE: usize = 3;
    const BORROWERS: usize = 12;
    let Some(database_url) = test_database_url() else {
        return Ok(());
    };
    let name = unique_application_name();
    let acquire_timeout = Duration::from_secs(2);
    let pool = Arc::new(Pool::build(
        &database_url,
        settings(&name, POOL_SIZE, acquire_timeout),
    )?);
    let observer = direct_connection(&database_url).await?;

    // Each borrower holds its connection until the gate opens, so the first
    // POOL_SIZE checkouts fill the pool while the rest wait.
    let (gate, gate_open) = tokio::sync::watch::channel(false);
    let (checked_out, mut checkouts) = tokio::sync::mpsc::unbounded_channel();
    let borrowers = (0..BORROWERS)
        .map(|_| {
            let pool = Arc::clone(&pool);
            let mut gate_open = gate_open.clone();
            let checked_out = checked_out.clone();
            tokio::spawn(async move {
                let client = pool.get().await?;
                let pid: i32 = client
                    .query_one("SELECT pg_backend_pid()", &[])
                    .await?
                    .get(0);
                checked_out.send(())?;
                gate_open.wait_for(|open| *open).await?;
                anyhow::Ok(pid)
            })
        })
        .collect::<Vec<_>>();
    drop(checked_out);
    for _ in 0..POOL_SIZE {
        checkouts
            .recv()
            .await
            .context("every borrower failed before filling the pool")?;
    }
    assert_eq!(
        server_connections(&observer, &name).await?,
        POOL_SIZE as i64,
        "{POOL_SIZE} borrowers hold every backend while the rest wait"
    );
    gate.send(true)?;
    let mut backends = HashSet::new();
    for borrower in borrowers {
        backends.insert(borrower.await??);
    }
    // A pool that ever opened a connection beyond its bound would show
    // another backend PID here.
    assert_eq!(
        backends.len(),
        POOL_SIZE,
        "{BORROWERS} borrowers share {POOL_SIZE} backends"
    );

    let _held = [pool.get().await?, pool.get().await?, pool.get().await?];
    let started = Instant::now();
    let waiter = pool.get().await;
    assert!(
        matches!(waiter, Err(PoolError::AcquireTimeout)),
        "a waiter past the deadline times out"
    );
    assert!(started.elapsed() >= acquire_timeout);
    assert_eq!(
        server_connections(&observer, &name).await?,
        POOL_SIZE as i64,
        "the timed-out waiter opened no connection"
    );
    Ok(())
}

#[tokio::test]
async fn build_and_connect_failures_are_typed() -> anyhow::Result<()> {
    let name = unique_application_name();
    let share = Pool::build(
        "postgresql://gobby@127.0.0.1/gobby_test",
        settings(&name, 1, Duration::from_secs(1)),
    );
    assert!(matches!(
        share,
        Err(PoolError::NativeShareTooSmall {
            actual: 1,
            minimum: 2
        })
    ));

    let closed_port = std::net::TcpListener::bind("127.0.0.1:0")?
        .local_addr()?
        .port();
    let password = "pool-unreachable-secret";
    let unreachable = Pool::build(
        &format!(
            "postgresql://gobby:{password}@127.0.0.1:{closed_port}/gobby_test?sslmode=disable"
        ),
        settings(&name, 2, Duration::from_secs(5)),
    )?;
    let Err(error) = unreachable.get().await else {
        anyhow::bail!("a closed port checked out a connection");
    };
    assert!(matches!(error, PoolError::Unavailable { .. }), "{error:?}");
    let text = error_chain(&error);
    assert!(
        text.contains(&format!("127.0.0.1:{closed_port}/gobby_test")),
        "{text}"
    );
    assert!(!text.contains(password), "the URL password leaked: {text}");

    let Some(database_url) = test_database_url() else {
        return Ok(());
    };
    let admin = direct_connection(&database_url).await?;
    let scratch = format!("gcore_pool_unit_{}", uuid::Uuid::new_v4().simple());
    let scratch_password = uuid::Uuid::new_v4().simple().to_string();
    admin
        .batch_execute(&format!(
            "CREATE ROLE {scratch} LOGIN PASSWORD '{scratch_password}'"
        ))
        .await?;
    // Once the role exists, cleanup runs before any error propagates.
    let outcome = match admin
        .batch_execute(&format!("CREATE DATABASE {scratch}"))
        .await
    {
        Ok(()) => unapplied_checkout(&database_url, &scratch, &scratch_password, &name).await,
        Err(error) => Err(error.into()),
    };
    admin
        .batch_execute(&format!("DROP DATABASE IF EXISTS {scratch} WITH (FORCE)"))
        .await?;
    admin
        .batch_execute(&format!("DROP ROLE IF EXISTS {scratch}"))
        .await?;

    let error = outcome?;
    assert!(
        matches!(error, PoolError::RuntimeRoleUnavailable { .. }),
        "{error:?}"
    );
    assert!(!error_chain(&error).contains(&scratch_password));
    Ok(())
}

/// Check out from an unapplied scratch database as a role with no
/// `gobby_daemon_runtime` membership, returning the checkout error.
async fn unapplied_checkout(
    database_url: &str,
    scratch: &str,
    password: &str,
    application_name: &str,
) -> anyhow::Result<PoolError> {
    let config: tokio_postgres::Config = database_url.parse()?;
    let host = match config.get_hosts().first() {
        Some(Host::Tcp(host)) => host.clone(),
        _ => anyhow::bail!("the test hub URL must name a TCP host"),
    };
    let port = config.get_ports().first().copied().unwrap_or(5432);
    let pool = Pool::build(
        &format!("postgresql://{scratch}:{password}@{host}:{port}/{scratch}?sslmode=disable"),
        settings(application_name, 2, Duration::from_secs(5)),
    )?;
    match pool.get().await {
        Ok(_) => anyhow::bail!("a role outside gobby_daemon_runtime checked out a connection"),
        Err(error) => Ok(error),
    }
}

#[tokio::test]
async fn stalled_hooks_are_bounded_and_release_capacity() -> anyhow::Result<()> {
    let Some(database_url) = test_database_url() else {
        return Ok(());
    };
    let name = unique_application_name();
    let acquire_timeout = Duration::from_secs(1);
    let pool = Pool::build(&database_url, settings(&name, 2, acquire_timeout))?;
    let _held = pool.get().await?;
    assert_eq!(pool.inner.status().size, 1);

    pool.create_stall.store(true, Ordering::SeqCst);
    let started = Instant::now();
    let stalled = pool.get().await;
    assert!(
        matches!(stalled, Err(PoolError::AcquireTimeout)),
        "stalled post_create"
    );
    assert!(started.elapsed() < Duration::from_secs(2));
    assert_eq!(
        pool.inner.status().size,
        1,
        "the stalled creation released its slot"
    );
    pool.create_stall.store(false, Ordering::SeqCst);

    let recovered = pool.get().await?;
    let (recycled_pid, role, zone, _) = session(&recovered).await?;
    assert_eq!(
        (role.as_str(), zone.as_str()),
        ("gobby_daemon_runtime", "UTC")
    );
    drop(recovered);

    pool.recycle_stall.store(true, Ordering::SeqCst);
    let started = Instant::now();
    let stalled = pool.get().await;
    assert!(
        matches!(stalled, Err(PoolError::AcquireTimeout)),
        "stalled post_recycle"
    );
    assert!(started.elapsed() < Duration::from_secs(2));
    assert_eq!(
        pool.inner.status().size,
        1,
        "the stalled recycle released its slot"
    );
    pool.recycle_stall.store(false, Ordering::SeqCst);

    let replacement = pool.get().await?;
    let (pid, role, zone, _) = session(&replacement).await?;
    assert_ne!(
        pid, recycled_pid,
        "the stalled connection never returns to the idle set"
    );
    assert_eq!(
        (role.as_str(), zone.as_str()),
        ("gobby_daemon_runtime", "UTC")
    );
    Ok(())
}

/// A family-style lock target; the seam itself defines none.
struct KeyedTarget {
    priority: i32,
    keys: &'static [&'static str],
}

impl LockTarget for KeyedTarget {
    fn priority(&self) -> i32 {
        self.priority
    }

    fn keys(&self) -> Vec<String> {
        self.keys.iter().map(|key| key.to_string()).collect()
    }
}

/// Poll a one-boolean query until it holds, for at most ten seconds.
async fn wait_until(
    observer: &tokio_postgres::Client,
    what: &str,
    query: &str,
    params: &[&(dyn ToSql + Sync)],
) -> anyhow::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !observer
        .query_one(query, params)
        .await?
        .try_get::<_, bool>(0)?
    {
        anyhow::ensure!(Instant::now() < deadline, "timed out waiting for {what}");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    Ok(())
}

/// Wait, for at most ten seconds, until the pool holds `size` connections.
async fn wait_for_size(pool: &Pool, size: usize) -> anyhow::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(10);
    while pool.status().size != size {
        anyhow::ensure!(
            Instant::now() < deadline,
            "pool size stayed at {}, expected {size}",
            pool.status().size
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    Ok(())
}

/// Terminate backend `pid` and return once it has exited.
async fn terminate(observer: &tokio_postgres::Client, pid: i32) -> anyhow::Result<()> {
    let exited: bool = observer
        .query_one("SELECT pg_terminate_backend($1, 5000)", &[&pid])
        .await?
        .try_get(0)?;
    anyhow::ensure!(exited, "backend {pid} did not exit");
    Ok(())
}

async fn backend_pid(transaction: &Transaction<'_>) -> anyhow::Result<i32> {
    Ok(transaction
        .query_one("SELECT pg_backend_pid()", &[])
        .await?
        .try_get(0)?)
}

/// Assign the transaction an id, so another session can read its outcome.
async fn transaction_id(transaction: &Transaction<'_>) -> anyhow::Result<String> {
    Ok(transaction
        .query_one("SELECT pg_current_xact_id()::text", &[])
        .await?
        .try_get(0)?)
}

/// `committed` or `aborted`, as the server recorded the transaction.
async fn transaction_status(
    observer: &tokio_postgres::Client,
    xid: &str,
) -> anyhow::Result<String> {
    Ok(observer
        .query_one("SELECT pg_xact_status($1::text::xid8)", &[&xid])
        .await?
        .try_get(0)?)
}

async fn next_checkout_pid(pool: &Pool) -> anyhow::Result<i32> {
    Ok(session(&pool.get().await?).await?.0)
}

/// A callback that records that it ran.
fn mark(ran: &Arc<AtomicBool>) -> Box<dyn FnOnce() -> anyhow::Result<()> + Send> {
    let ran = ran.clone();
    Box::new(move || {
        ran.store(true, Ordering::SeqCst);
        Ok(())
    })
}

fn transaction_error<T>(outcome: anyhow::Result<T>) -> Option<TransactionError> {
    outcome.err()?.downcast().ok()
}

#[tokio::test]
async fn transaction_commits_and_runs_callbacks() -> anyhow::Result<()> {
    let Some(database_url) = test_database_url() else {
        return Ok(());
    };
    let name = unique_application_name();
    let pool = Arc::new(Pool::build(
        &database_url,
        settings(&name, 2, Duration::from_secs(5)),
    )?);
    let _held = pool.get().await?;
    let observer = direct_connection(&database_url).await?;

    let order = Arc::new(Mutex::new(Vec::new()));
    let available = Arc::new(Mutex::new(None));
    let signal = Arc::new(Notify::new());
    let committed: anyhow::Result<(&str, String)> = pool
        .transaction(None, async |transaction| {
            let xid = transaction_id(transaction).await?;
            let (first, observed, pool) = (order.clone(), available.clone(), pool.clone());
            transaction.after_commit(Box::new(move || {
                *observed.lock().unwrap() = Some(pool.status().available);
                first.lock().unwrap().push(1);
                Ok(())
            }));
            let second = order.clone();
            transaction.after_commit(Box::new(move || -> anyhow::Result<()> {
                second.lock().unwrap().push(2);
                anyhow::bail!("callback 2 fails")
            }));
            let (third, signal) = (order.clone(), signal.clone());
            transaction.after_commit(Box::new(move || {
                third.lock().unwrap().push(3);
                signal.notify_one();
                Ok(())
            }));
            Ok(("result", xid))
        })
        .await;
    let (result, xid) = committed?;
    tokio::time::timeout(Duration::from_secs(5), signal.notified()).await?;
    drop(pool.get().await?);
    assert_eq!(
        result, "result",
        "a failing callback leaves the result unchanged"
    );
    assert_eq!(transaction_status(&observer, &xid).await?, "committed");
    assert_eq!(
        *order.lock().unwrap(),
        [1, 2, 3],
        "callbacks run in order, past a failing one"
    );
    assert_eq!(
        *available.lock().unwrap(),
        Some(1),
        "the checkout is released before callbacks run"
    );

    let ran = Arc::new(AtomicBool::new(false));
    let mut rolled_back_xid = String::new();
    let rolled_back: anyhow::Result<()> = pool
        .transaction(None, async |transaction| {
            transaction.after_commit(mark(&ran));
            rolled_back_xid = transaction_id(transaction).await?;
            anyhow::bail!("the closure fails")
        })
        .await;
    assert_eq!(
        rolled_back.expect_err("an Err closure").to_string(),
        "the closure fails"
    );
    assert_eq!(
        transaction_status(&observer, &rolled_back_xid).await?,
        "aborted"
    );
    assert!(
        !ran.load(Ordering::SeqCst),
        "a rolled-back transaction runs no callbacks"
    );

    let lost: anyhow::Result<()> = pool
        .transaction(None, async |transaction| {
            transaction.after_commit(mark(&ran));
            terminate(&observer, backend_pid(transaction).await?).await
        })
        .await;
    assert!(
        matches!(
            transaction_error(lost),
            Some(TransactionError::IndeterminateCommit(_))
        ),
        "a COMMIT on a terminated backend is indeterminate"
    );
    assert!(
        !ran.load(Ordering::SeqCst),
        "an IndeterminateCommit runs no callbacks"
    );
    Ok(())
}

#[tokio::test]
async fn commit_outcome_is_classified() -> anyhow::Result<()> {
    let Some(database_url) = test_database_url() else {
        return Ok(());
    };
    let name = unique_application_name();
    let pool = Pool::build(&database_url, settings(&name, 2, Duration::from_secs(5)))?;
    let _held = pool.get().await?;
    let observer = direct_connection(&database_url).await?;

    let mut rejected_pid = 0;
    let rejected: anyhow::Result<()> = pool
        .transaction(None, async |transaction| {
            rejected_pid = backend_pid(transaction).await?;
            transaction
                .execute(
                    "CREATE TEMP TABLE commit_rejection \
                     (id int UNIQUE DEFERRABLE INITIALLY DEFERRED)",
                    &[],
                )
                .await?;
            transaction
                .execute("INSERT INTO commit_rejection VALUES (1), (1)", &[])
                .await?;
            Ok(())
        })
        .await;
    match transaction_error(rejected) {
        Some(TransactionError::Server(error)) => {
            assert_eq!(error.code(), Some(&SqlState::UNIQUE_VIOLATION));
        }
        other => panic!("a deferred unique violation at COMMIT is Server, got {other:?}"),
    }
    assert_eq!(
        next_checkout_pid(&pool).await?,
        rejected_pid,
        "a definite rejection keeps its connection"
    );

    let mut terminated_pid = 0;
    let lost: anyhow::Result<()> = pool
        .transaction(None, async |transaction| {
            terminated_pid = backend_pid(transaction).await?;
            terminate(&observer, terminated_pid).await
        })
        .await;
    assert!(
        matches!(
            transaction_error(lost),
            Some(TransactionError::IndeterminateCommit(_))
        ),
        "a COMMIT whose backend died is indeterminate"
    );
    assert_ne!(
        next_checkout_pid(&pool).await?,
        terminated_pid,
        "an indeterminate COMMIT discards its connection"
    );
    Ok(())
}

#[tokio::test]
async fn cancelled_commit_discards_connection() -> anyhow::Result<()> {
    let Some(database_url) = test_database_url() else {
        return Ok(());
    };
    let name = unique_application_name();
    let pool = Arc::new(Pool::build(
        &database_url,
        settings(&name, 2, Duration::from_secs(5)),
    )?);
    let _held = pool.get().await?;
    let control = direct_connection(&database_url).await?;
    // A per-run key, so concurrent runs never contend.
    let key = i64::from_le_bytes(uuid::Uuid::new_v4().as_bytes()[..8].try_into()?);
    control
        .execute("SELECT pg_advisory_lock($1)", &[&key])
        .await?;

    let ran = Arc::new(AtomicBool::new(false));
    let (pid_sender, pid_receiver) = oneshot::channel();
    let task = tokio::spawn({
        let (pool, ran) = (pool.clone(), ran.clone());
        async move {
            pool.transaction(None, async move |transaction| {
                transaction.after_commit(mark(&ran));
                let pid = backend_pid(transaction).await?;
                for statement in [
                    format!(
                        "CREATE FUNCTION pg_temp.block_commit() RETURNS trigger \
                         LANGUAGE plpgsql AS $$BEGIN \
                         PERFORM pg_advisory_xact_lock({key}); RETURN NULL; END$$"
                    ),
                    "CREATE TEMP TABLE commit_blocker (id int)".to_string(),
                    "CREATE CONSTRAINT TRIGGER commit_blocker AFTER INSERT ON commit_blocker \
                     DEFERRABLE INITIALLY DEFERRED FOR EACH ROW \
                     EXECUTE FUNCTION pg_temp.block_commit()"
                        .to_string(),
                    "INSERT INTO commit_blocker VALUES (1)".to_string(),
                ] {
                    transaction.execute(&statement, &[]).await?;
                }
                pid_sender
                    .send(pid)
                    .map_err(|_| anyhow::anyhow!("the test stopped listening"))?;
                Ok::<(), anyhow::Error>(())
            })
            .await
        }
    });
    let pid = tokio::time::timeout(Duration::from_secs(10), pid_receiver).await??;
    wait_until(
        &control,
        "COMMIT to wait on the advisory lock",
        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity \
         WHERE pid = $1 AND query = 'COMMIT' AND wait_event_type = 'Lock')",
        &[&pid],
    )
    .await?;
    task.abort();
    assert!(
        task.await.is_err_and(|error| error.is_cancelled()),
        "the transaction is cancelled mid-COMMIT"
    );
    wait_for_size(&pool, 1).await?;
    assert!(
        !ran.load(Ordering::SeqCst),
        "a cancelled COMMIT runs no callbacks"
    );

    control
        .execute("SELECT pg_advisory_unlock($1)", &[&key])
        .await?;
    wait_until(
        &control,
        "the abandoned backend to exit",
        "SELECT NOT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = $1)",
        &[&pid],
    )
    .await?;
    assert_ne!(
        next_checkout_pid(&pool).await?,
        pid,
        "the cancelled COMMIT's connection is never reused"
    );
    Ok(())
}

#[tokio::test]
async fn failed_rollback_and_armed_guard_discard() -> anyhow::Result<()> {
    let Some(database_url) = test_database_url() else {
        return Ok(());
    };
    let name = unique_application_name();
    let pool = Arc::new(Pool::build(
        &database_url,
        settings(&name, 2, Duration::from_secs(5)),
    )?);
    let _held = pool.get().await?;
    let observer = direct_connection(&database_url).await?;

    let ran = Arc::new(AtomicBool::new(false));
    let mut terminated_pid = 0;
    let failed: anyhow::Result<()> = pool
        .transaction(None, async |transaction| {
            transaction.after_commit(mark(&ran));
            terminated_pid = backend_pid(transaction).await?;
            terminate(&observer, terminated_pid).await?;
            anyhow::bail!("the closure's own error")
        })
        .await;
    assert_eq!(
        failed.expect_err("an Err closure").to_string(),
        "the closure's own error",
        "a failed ROLLBACK returns the closure's error"
    );
    assert!(
        !ran.load(Ordering::SeqCst),
        "a failed rollback runs no callbacks"
    );
    assert_ne!(
        next_checkout_pid(&pool).await?,
        terminated_pid,
        "a failed rollback discards its connection"
    );

    let (reached, at_gate) = oneshot::channel();
    *pool.rollback_gate.lock().unwrap() = Some(reached);
    let (pid_sender, pid_receiver) = oneshot::channel();
    let task = tokio::spawn({
        let (pool, ran) = (pool.clone(), ran.clone());
        async move {
            pool.transaction(None, async move |transaction| {
                transaction.after_commit(mark(&ran));
                pid_sender
                    .send(backend_pid(transaction).await?)
                    .map_err(|_| anyhow::anyhow!("the test stopped listening"))?;
                Err::<(), _>(anyhow::anyhow!("roll back"))
            })
            .await
        }
    });
    let pid = tokio::time::timeout(Duration::from_secs(10), pid_receiver).await??;
    tokio::time::timeout(Duration::from_secs(10), at_gate).await??;
    task.abort();
    assert!(
        task.await.is_err_and(|error| error.is_cancelled()),
        "the transaction is cancelled at the rollback gate"
    );
    wait_for_size(&pool, 1).await?;
    assert!(
        !ran.load(Ordering::SeqCst),
        "a cancelled rollback runs no callbacks"
    );
    assert_ne!(
        next_checkout_pid(&pool).await?,
        pid,
        "an armed guard discards its connection when dropped"
    );
    Ok(())
}

#[tokio::test]
async fn lock_targets_share_python_keys_and_order() -> anyhow::Result<()> {
    const TASK: KeyedTarget = KeyedTarget {
        priority: 10,
        keys: &["task_lifecycle:t1"],
    };
    const SIBLING: KeyedTarget = KeyedTarget {
        priority: 10,
        keys: &["task_lifecycle:t2"],
    };
    const OUTER: KeyedTarget = KeyedTarget {
        priority: 5,
        keys: &["task_seq:p1"],
    };
    const EMPTY: KeyedTarget = KeyedTarget {
        priority: 20,
        keys: &[],
    };
    let Some(database_url) = test_database_url() else {
        return Ok(());
    };
    let name = unique_application_name();
    let pool = Pool::build(&database_url, settings(&name, 2, Duration::from_secs(5)))?;
    let observer = direct_connection(&database_url).await?;
    let contender = direct_connection(&database_url).await?;
    let (contender_pid, ..) = session(&contender).await?;

    let committed: anyhow::Result<_> = pool
        .transaction(Some(&TASK), async |_transaction| {
            let waiter = tokio::spawn(async move {
                contender
                    .batch_execute(
                        "BEGIN; SELECT pg_advisory_xact_lock(hashtext('task_lifecycle:t1')); \
                         COMMIT",
                    )
                    .await
            });
            wait_until(
                &observer,
                "the Python-keyed lock to block a second connection",
                "SELECT EXISTS (SELECT 1 FROM pg_locks \
                 WHERE pid = $1 AND locktype = 'advisory' AND NOT granted)",
                &[&contender_pid],
            )
            .await?;
            Ok(waiter)
        })
        .await;
    tokio::time::timeout(Duration::from_secs(10), committed?).await???;

    let checked: anyhow::Result<()> = pool
        .transaction(Some(&TASK), async |transaction| {
            // From here on the transaction is aborted, so any statement the
            // seam sends fails with 25P02.
            assert!(transaction.execute("SELECT 1 / 0", &[]).await.is_err());
            transaction.acquire_lock(&TASK).await?;
            for lower in [&SIBLING, &OUTER] {
                assert!(
                    matches!(
                        transaction.acquire_lock(lower).await,
                        Err(TransactionError::LockOrder { .. })
                    ),
                    "priority {} after {}",
                    lower.priority,
                    TASK.priority
                );
            }
            assert!(matches!(
                transaction.acquire_lock(&EMPTY).await,
                Err(TransactionError::EmptyLockTarget)
            ));
            let sent = transaction.execute("SELECT 1", &[]).await;
            assert!(
                matches!(
                    sent,
                    Err(TransactionError::Server(ref error))
                        if error.code() == Some(&SqlState::IN_FAILED_SQL_TRANSACTION)
                ),
                "a statement sent now fails, so the checks above sent none"
            );
            anyhow::bail!("roll back the aborted transaction")
        })
        .await;
    assert_eq!(
        checked.expect_err("an Err closure").to_string(),
        "roll back the aborted transaction"
    );
    Ok(())
}

#[test]
fn identifiers_match_python_validation() {
    for (name, quoted) in [("tasks", "\"tasks\""), ("_x9", "\"_x9\"")] {
        assert_eq!(
            quote_identifier(name).ok().as_deref(),
            Some(quoted),
            "`{name}` is valid"
        );
    }
    for name in ["9x", "a-b", "a b", "\"q\"", ""] {
        assert!(
            matches!(quote_identifier(name), Err(IdentifierError(refused)) if refused == name),
            "`{name}` is refused"
        );
    }
}

#[derive(Debug, PartialEq)]
struct Probe {
    id: i32,
    name: String,
    note: Option<String>,
}

impl FromRow for Probe {
    fn from_row(row: &tokio_postgres::Row) -> Result<Self, RowError> {
        Ok(Self {
            id: row.try_get("id")?,
            name: row.try_get("name")?,
            note: row.try_get("note")?,
        })
    }
}

#[tokio::test]
async fn from_row_maps_rows() -> anyhow::Result<()> {
    let Some(database_url) = test_database_url() else {
        return Ok(());
    };
    let name = unique_application_name();
    let pool = Pool::build(&database_url, settings(&name, 2, Duration::from_secs(5)))?;
    const PROBES: &str = "SELECT * FROM (VALUES (1, 'one', NULL::text), (2, 'two', 'second')) \
                          AS probe(id, name, note) WHERE id <= $1 ORDER BY id";

    let mapped: anyhow::Result<_> = pool
        .transaction(None, async |transaction| {
            Ok((
                transaction.query_as::<Probe>(PROBES, &[&2_i32]).await?,
                transaction.query_opt_as::<Probe>(PROBES, &[&1_i32]).await?,
                transaction.query_opt_as::<Probe>(PROBES, &[&0_i32]).await?,
                transaction
                    .query_as::<Probe>("SELECT 'x' AS id, 'one' AS name, NULL::text AS note", &[])
                    .await,
            ))
        })
        .await;
    let (all, one, none, mismatched) = mapped?;
    let first = Probe {
        id: 1,
        name: "one".to_string(),
        note: None,
    };
    let second = Probe {
        id: 2,
        name: "two".to_string(),
        note: Some("second".to_string()),
    };
    assert_eq!(all, [first, second]);
    assert_eq!(one.map(|probe| probe.id), Some(1));
    assert_eq!(none, None);
    assert!(
        matches!(mismatched, Err(TransactionError::Row(_))),
        "a column of the wrong type is a row error"
    );
    Ok(())
}

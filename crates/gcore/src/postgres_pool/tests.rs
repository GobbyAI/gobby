use super::*;
use anyhow::Context;
use std::collections::HashSet;
use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::Instant;
use tokio_postgres::NoTls;
use tokio_postgres::config::Host;

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

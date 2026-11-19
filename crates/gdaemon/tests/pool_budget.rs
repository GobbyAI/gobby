//! The native pool budget's inputs, read from the isolated test hub through a pool that
//! runs as the daemon's runtime role in a schema of its own.

use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, ensure};
use gobby_core::database_concurrency::PostgresCapacity;
use gobby_core::database_concurrency::pool_budget::StoredKeys;
use gobby_core::postgres_pool::{Pool, PoolSettings};
use gobby_daemon::native_pools::{build_serve, build_set, read_budget_inputs};
use tokio_postgres::NoTls;
use uuid::Uuid;

/// The hub's own capacity settings, read on the admin connection.
async fn hub_capacity(admin: &tokio_postgres::Client) -> Result<PostgresCapacity> {
    let row = admin
        .query_one(
            "SELECT current_setting('max_connections')::integer, \
             current_setting('superuser_reserved_connections')::integer, \
             COALESCE(current_setting('reserved_connections', true), '0')::integer",
            &[],
        )
        .await?;
    let setting = |index: usize| -> Result<u32> {
        let value: i32 = row.try_get(index)?;
        Ok(u32::try_from(value)?)
    };
    Ok(PostgresCapacity {
        max_connections: setting(0)?,
        superuser_reserved_connections: setting(1)?,
        reserved_connections: setting(2)?,
    })
}

#[tokio::test]
async fn reads_capacity_and_keys_from_the_hub() -> Result<()> {
    let url = std::env::var("DATABASE_URL").context("DATABASE_URL names the test hub")?;
    let config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("gobby_test")
            && config.get_ports() == [60892]
            && std::env::var("GOBBY_TEST_PROTECT").as_deref() == Ok("1"),
        "the pool budget test requires the protected isolated test hub"
    );
    let (admin, driver) = config.connect(NoTls).await?;
    let driver = tokio::spawn(driver);
    let schema = format!("test_pool_budget_{}", Uuid::new_v4().simple());
    admin
        .batch_execute(&format!(
            "CREATE SCHEMA {schema}; GRANT USAGE ON SCHEMA {schema} TO gobby_daemon_runtime; \
             CREATE TABLE {schema}.config_store(key text PRIMARY KEY, value text NOT NULL); \
             GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO gobby_daemon_runtime"
        ))
        .await?;
    let separator = if url.contains('?') { '&' } else { '?' };
    let pool = Pool::build(
        &format!("{url}{separator}options=-csearch_path%3D{schema}"),
        PoolSettings {
            max_size: 2,
            application_name: "gobby-gdaemon-pool-budget-test".into(),
            acquire_timeout: Duration::from_secs(5),
        },
    )?;

    let outcome = async {
        let expected = hub_capacity(&admin).await?;
        let (keys, capacity) = read_budget_inputs(&pool).await?;
        assert_eq!(capacity, expected);
        assert_eq!(keys, StoredKeys::default());

        for (key, raw) in [
            ("database_concurrency.pool_max_size", "72"),
            ("database_concurrency.coverage_max_concurrency", "\"auto\""),
            ("database_concurrency.executor_max_workers", "16"),
        ] {
            admin
                .execute(
                    &format!("INSERT INTO {schema}.config_store(key, value) VALUES ($1, $2)"),
                    &[&key, &raw],
                )
                .await?;
        }
        let (keys, _) = read_budget_inputs(&pool).await?;
        assert_eq!(
            keys,
            StoredKeys {
                pool_max_size: Some("72".into()),
                executor_max_workers: Some("16".into()),
                coverage_max_concurrency: Some("\"auto\"".into()),
            }
        );
        Ok::<(), anyhow::Error>(())
    };
    let outcome = std::panic::AssertUnwindSafe(outcome);
    let outcome = futures_util::FutureExt::catch_unwind(outcome).await;
    drop(pool);
    let cleanup = admin
        .batch_execute(&format!("DROP SCHEMA {schema} CASCADE"))
        .await;
    drop(admin);
    driver.abort();
    let _ = driver.await;
    match outcome {
        Ok(result) => {
            cleanup?;
            result
        }
        Err(panic) => std::panic::resume_unwind(panic),
    }
}

/// Three tasks that each hold one checkout for `hold` on `pool`; returns how long each
/// waited for its checkout.
async fn three_holders(pool: Arc<Pool>, hold: Duration) -> Result<Vec<Duration>> {
    let holders: Vec<_> = (0..3)
        .map(|_| {
            let pool = Arc::clone(&pool);
            tokio::spawn(async move {
                let asked = Instant::now();
                let client = pool.get().await?;
                let waited = asked.elapsed();
                client.simple_query("SELECT 1").await?;
                tokio::time::sleep(hold).await;
                drop(client);
                Ok::<Duration, anyhow::Error>(waited)
            })
        })
        .collect();
    let mut waits = Vec::with_capacity(holders.len());
    for holder in holders {
        waits.push(holder.await??);
    }
    Ok(waits)
}

#[tokio::test]
async fn concurrent_checkouts_fit_the_share() -> Result<()> {
    let url = std::env::var("DATABASE_URL").context("DATABASE_URL names the test hub")?;
    let config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("gobby_test")
            && config.get_ports() == [60892]
            && std::env::var("GOBBY_TEST_PROTECT").as_deref() == Ok("1"),
        "the pool share test requires the protected isolated test hub"
    );
    let set = build_set(&url, true)?;
    let writes = set
        .terminal_writes
        .context("an owner builds the terminal writes pool")?;
    let authority = set
        .terminal_authority
        .context("an owner builds the terminal authority pool")?;
    let pools = [
        ("serve", build_serve(&url)?, Duration::from_secs(5)),
        ("active", set.active, Duration::from_secs(2)),
        ("terminal writes", writes, Duration::from_secs(2)),
        ("terminal authority", authority, Duration::from_secs(2)),
    ];
    let hold = Duration::from_millis(200);

    for (name, pool, deadline) in pools {
        let waits = three_holders(Arc::new(pool), hold).await?;

        assert_eq!(waits.len(), 3, "{name}: every holder checked out");
        let slowest = waits.iter().max().copied().unwrap_or_default();
        assert!(
            slowest < deadline,
            "{name}: a checkout waited {slowest:?}, beyond its {deadline:?} deadline"
        );
    }
    Ok(())
}

//! The native pool budget's inputs, read from the isolated test hub through a pool that
//! runs as the daemon's runtime role in a schema of its own.

use std::time::Duration;

use anyhow::{Context, Result, ensure};
use gobby_core::database_concurrency::PostgresCapacity;
use gobby_core::database_concurrency::pool_budget::StoredKeys;
use gobby_core::postgres_pool::{Pool, PoolSettings};
use gobby_daemon::native_pools::read_budget_inputs;
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

//! The daemon's native PostgreSQL pools, each built lazily at its share of the native
//! budget, and the budget outcome read from the hub.

use std::time::Duration;

use anyhow::{Context, Result};
use gobby_core::database_concurrency::PostgresCapacity;
use gobby_core::database_concurrency::pool_budget::{
    POOL_SHARE, PoolBudget, StoredKeys, resolve_pool_budget,
};
use gobby_core::postgres_pool::{Pool, PoolSettings};

/// The pools that exist while the native runtime holds the lease.
pub struct NativePools {
    /// Lease-scoped work: `gobby-gdaemon-active`.
    pub active: Pool,
    /// Terminal writes, built only when the daemon owns the terminals.
    pub terminal_writes: Option<Pool>,
    /// The terminal epoch authority, built only when the daemon owns the terminals.
    pub terminal_authority: Option<Pool>,
}

/// How long a lease-scoped checkout may wait.
const LEASE_ACQUIRE: Duration = Duration::from_secs(2);

/// The degraded name of a pool budget that could not be read.
const DEGRADED_POOL: &str = "database_pool";

/// The keys whose stored values size the pools, in [`StoredKeys`] field order.
const BUDGET_KEYS: [&str; 3] = [
    "database_concurrency.pool_max_size",
    "database_concurrency.executor_max_workers",
    "database_concurrency.coverage_max_concurrency",
];

/// Builds one lazy pool at the native share.
fn build(url: &str, application_name: &str, acquire_timeout: Duration) -> Result<Pool> {
    Ok(Pool::build(
        url,
        PoolSettings {
            max_size: usize::try_from(POOL_SHARE)?,
            application_name: application_name.into(),
            acquire_timeout,
        },
    )?)
}

/// The serve pool, `gobby-gdaemon`, which the front door's families share.
pub fn build_serve(url: &str) -> Result<Pool> {
    build(url, "gobby-gdaemon", Duration::from_secs(5))
}

/// The lease-scoped pools. A daemon that owns the terminals also builds their two pools.
pub fn build_set(url: &str, owner: bool) -> Result<NativePools> {
    let active = build(url, "gobby-gdaemon-active", LEASE_ACQUIRE)?;
    if !owner {
        return Ok(NativePools {
            active,
            terminal_writes: None,
            terminal_authority: None,
        });
    }
    Ok(NativePools {
        active,
        terminal_writes: Some(build(url, "gobby-gdaemon-terminal-writes", LEASE_ACQUIRE)?),
        terminal_authority: Some(build(url, "gobby-gdaemon-terminals", LEASE_ACQUIRE)?),
    })
}

/// What the budget read at start decided.
#[derive(Debug)]
pub enum PoolOutcome {
    /// The budget resolved; its `degraded` mark says whether every pool gets its share.
    Ready(PoolBudget),
    /// The read failed, so the pools run without a resolved budget.
    Degraded { reason: &'static str },
    /// The stored values are refused by the resolver.
    Refused { message: String },
}

/// Reads the three stored `database_concurrency` keys and the hub's connection capacity,
/// each on its own checkout, one after the other, so the read holds one connection at a
/// time.
pub async fn read_budget_inputs(pool: &Pool) -> Result<(StoredKeys, PostgresCapacity)> {
    let stored = read_stored_keys(pool).await?;
    let capacity = read_capacity(pool).await?;
    Ok((stored, capacity))
}

async fn read_stored_keys(pool: &Pool) -> Result<StoredKeys> {
    let client = pool.get().await?;
    let rows = client
        .query(
            "SELECT key, value FROM config_store WHERE key = ANY($1)",
            &[&BUDGET_KEYS.as_slice()],
        )
        .await
        .context("reading the stored database_concurrency keys")?;
    let mut values: [Option<String>; 3] = Default::default();
    for row in rows {
        let key: String = row.try_get(0)?;
        let value: String = row.try_get(1)?;
        if let Some(index) = BUDGET_KEYS.iter().position(|known| *known == key) {
            values[index] = Some(value);
        }
    }
    let [
        pool_max_size,
        executor_max_workers,
        coverage_max_concurrency,
    ] = values;
    Ok(StoredKeys {
        pool_max_size,
        executor_max_workers,
        coverage_max_concurrency,
    })
}

/// The server's connection settings, read as the Python hub reads them: the two required
/// settings must be reported, and `reserved_connections` is 0 on a server without it.
async fn read_capacity(pool: &Pool) -> Result<PostgresCapacity> {
    let client = pool.get().await?;
    let rows = client
        .query(
            "SELECT name, setting FROM pg_settings WHERE name IN \
             ('max_connections', 'superuser_reserved_connections', 'reserved_connections')",
            &[],
        )
        .await
        .context("reading the hub's connection settings")?;
    let mut settings = std::collections::BTreeMap::new();
    for row in rows {
        let name: String = row.try_get(0)?;
        let setting: String = row.try_get(1)?;
        let value: u32 = setting
            .parse()
            .with_context(|| format!("PostgreSQL setting {name} is not a count: {setting}"))?;
        settings.insert(name, value);
    }
    let required = |name: &str| {
        settings
            .get(name)
            .copied()
            .with_context(|| format!("PostgreSQL did not report {name}"))
    };
    Ok(PostgresCapacity {
        max_connections: required("max_connections")?,
        superuser_reserved_connections: required("superuser_reserved_connections")?,
        reserved_connections: settings.get("reserved_connections").copied().unwrap_or(0),
    })
}

/// The outcome of one budget read: a refusal of the stored values is `Refused`, and a
/// failed read is `Degraded`.
pub fn outcome_for(read: Result<(StoredKeys, PostgresCapacity)>, cpu_count: u32) -> PoolOutcome {
    let Ok((stored, capacity)) = read else {
        return PoolOutcome::Degraded {
            reason: DEGRADED_POOL,
        };
    };
    match resolve_pool_budget(&stored, &capacity, cpu_count) {
        Ok(budget) => PoolOutcome::Ready(budget),
        Err(error) => PoolOutcome::Refused {
            message: error.to_string(),
        },
    }
}

/// Reads the budget inputs from the hub and decides the outcome.
pub async fn resolve_outcome(pool: &Pool, cpu_count: u32) -> PoolOutcome {
    outcome_for(read_budget_inputs(pool).await, cpu_count)
}

#[cfg(test)]
mod tests {
    use super::*;

    const LAZY_URL: &str = "postgresql://gobby_daemon_runtime@127.0.0.1:9/gobby";

    fn capacity() -> PostgresCapacity {
        PostgresCapacity {
            max_connections: 100,
            superuser_reserved_connections: 3,
            reserved_connections: 0,
        }
    }

    fn max_sizes(pools: &NativePools) -> Vec<Option<usize>> {
        vec![
            Some(pools.active.status().max_size),
            pools
                .terminal_writes
                .as_ref()
                .map(|pool| pool.status().max_size),
            pools
                .terminal_authority
                .as_ref()
                .map(|pool| pool.status().max_size),
        ]
    }

    #[tokio::test]
    async fn built_pools_take_their_share() {
        let serve = build_serve(LAZY_URL).expect("the serve pool builds lazily");
        assert_eq!(serve.status().max_size, 2);

        let owner = build_set(LAZY_URL, true).expect("the owner set builds lazily");
        assert_eq!(max_sizes(&owner), vec![Some(2), Some(2), Some(2)]);

        let follower = build_set(LAZY_URL, false).expect("the follower set builds lazily");
        assert_eq!(max_sizes(&follower), vec![Some(2), None, None]);
    }

    #[test]
    fn outcome_for_separates_refusal_from_degradation() {
        let refused_keys = StoredKeys {
            pool_max_size: Some("\"72\"".into()),
            ..StoredKeys::default()
        };
        let refused = outcome_for(Ok((refused_keys, capacity())), 8);
        assert!(
            matches!(refused, PoolOutcome::Refused { .. }),
            "a stored string integer is refused: {refused:?}"
        );

        let degraded = outcome_for(Err(anyhow::anyhow!("unreachable")), 8);
        assert!(
            matches!(
                degraded,
                PoolOutcome::Degraded {
                    reason: "database_pool"
                }
            ),
            "a failed read is degraded: {degraded:?}"
        );
    }
}

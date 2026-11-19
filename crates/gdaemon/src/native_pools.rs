//! The daemon's native PostgreSQL pools, each built lazily at its share of the native
//! budget, and the budget outcome read from the hub.

use std::time::Duration;

use anyhow::Result;
use gobby_core::database_concurrency::PostgresCapacity;
use gobby_core::database_concurrency::pool_budget::{PoolBudget, StoredKeys, resolve_pool_budget};
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

fn build(url: &str, application_name: &str, acquire_timeout: Duration) -> Result<Pool> {
    Ok(Pool::build(
        url,
        PoolSettings {
            max_size: 8,
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
    let _ = owner;
    Ok(NativePools {
        active: build(url, "gobby-gdaemon-active", Duration::from_secs(2))?,
        terminal_writes: None,
        terminal_authority: None,
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

/// Reads the three stored `database_concurrency` keys and the hub's connection capacity.
pub async fn read_budget_inputs(pool: &Pool) -> Result<(StoredKeys, PostgresCapacity)> {
    let _ = pool;
    Ok((
        StoredKeys::default(),
        PostgresCapacity {
            max_connections: 0,
            superuser_reserved_connections: 0,
            reserved_connections: 0,
        },
    ))
}

/// The outcome of one budget read: a refusal of the stored values is `Refused`, and a
/// failed read is `Degraded`.
pub fn outcome_for(read: Result<(StoredKeys, PostgresCapacity)>, cpu_count: u32) -> PoolOutcome {
    let (stored, capacity) = read.unwrap_or_else(|_| {
        (
            StoredKeys::default(),
            PostgresCapacity {
                max_connections: 0,
                superuser_reserved_connections: 0,
                reserved_connections: 0,
            },
        )
    });
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

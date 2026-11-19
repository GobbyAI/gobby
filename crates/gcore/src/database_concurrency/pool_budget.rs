//! The native pool budget: the three stored `database_concurrency` keys, resolved against
//! the hub's capacity, and the fixed share each native pool takes of it.

use super::{
    ConcurrencyValue, DatabaseConcurrencyConfig, DatabaseConcurrencyError,
    DatabaseConcurrencyResolution, PostgresCapacity,
};

/// The connections each native pool takes: the smallest share the pool accepts.
pub const POOL_SHARE: u32 = 2;

/// The native pools that share the budget: serve, active, terminal writes and terminal
/// authority.
const NATIVE_POOL_COUNT: u32 = 4;

/// The raw text of each key as stored in `config_store.value`; `None` when no row exists.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct StoredKeys {
    pub pool_max_size: Option<String>,
    pub executor_max_workers: Option<String>,
    pub coverage_max_concurrency: Option<String>,
}

/// The resolved budget and the share each native pool takes of it.
#[derive(Clone, Debug, PartialEq)]
pub struct PoolBudget {
    /// The resolver's full output.
    pub resolution: DatabaseConcurrencyResolution,
    /// The connections left for the native pools.
    pub native_budget: u32,
    /// The share each native pool takes, [`POOL_SHARE`].
    pub share: u32,
    /// `Some("database_pool")` when the native budget cannot give every native pool its
    /// share.
    pub degraded: Option<&'static str>,
}

/// Parses one stored value as strict JSON. An absent value is automatic; the JSON string
/// `"auto"` is automatic; a JSON integer of 1 or more is explicit. A JSON integer below 1
/// is `NonPositiveValue`, and any other text is `InvalidStoredValue`.
pub fn parse_stored(
    field: &'static str,
    raw: Option<&str>,
) -> Result<ConcurrencyValue, DatabaseConcurrencyError> {
    let _ = (field, raw);
    Ok(ConcurrencyValue::default())
}

/// The resolver's input from the three stored keys.
pub fn to_config(
    stored: &StoredKeys,
) -> Result<DatabaseConcurrencyConfig, DatabaseConcurrencyError> {
    Ok(DatabaseConcurrencyConfig {
        pool_max_size: parse_stored(
            "database_concurrency.pool_max_size",
            stored.pool_max_size.as_deref(),
        )?,
        executor_max_workers: parse_stored(
            "database_concurrency.executor_max_workers",
            stored.executor_max_workers.as_deref(),
        )?,
        coverage_max_concurrency: parse_stored(
            "database_concurrency.coverage_max_concurrency",
            stored.coverage_max_concurrency.as_deref(),
        )?,
    })
}

/// Resolves the stored keys against the hub's capacity and splits the native budget.
pub fn resolve_pool_budget(
    stored: &StoredKeys,
    capacity: &PostgresCapacity,
    cpu_count: u32,
) -> Result<PoolBudget, DatabaseConcurrencyError> {
    let _ = (to_config(stored)?, NATIVE_POOL_COUNT);
    Ok(PoolBudget {
        resolution: DatabaseConcurrencyResolution {
            cpu_count,
            max_connections: capacity.max_connections,
            superuser_reserved_connections: capacity.superuser_reserved_connections,
            reserved_connections: capacity.reserved_connections,
            usable_connections: 0,
            pool_budget: 0,
            pool_max_size: 0,
            native_pool_max_size: 0,
            executor_max_workers: 0,
            coverage_max_concurrency: 0,
            direct_connection_reserve: 0,
            bootstrap_pool_size: 0,
            hardware_warning: None,
        },
        native_budget: 0,
        share: 8,
        degraded: None,
    })
}

#[cfg(test)]
mod tests;

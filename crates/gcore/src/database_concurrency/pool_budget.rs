//! The native pool budget: the three stored `database_concurrency` keys, resolved against
//! the hub's capacity, and the fixed share each native pool takes of it.

use serde_json::Value;

use super::{
    ConcurrencyValue, DatabaseConcurrencyConfig, DatabaseConcurrencyError,
    DatabaseConcurrencyResolution, PostgresCapacity, resolve_database_concurrency,
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
    let Some(raw) = raw else {
        return Ok(ConcurrencyValue::default());
    };
    let invalid = || DatabaseConcurrencyError::InvalidStoredValue { field };
    match serde_json::from_str::<Value>(raw).map_err(|_| invalid())? {
        Value::String(text) if text == "auto" => Ok(ConcurrencyValue::Automatic(text)),
        // A float or an integer beyond i64 has no `as_i64` and is not a stored count.
        Value::Number(number) => match number.as_i64() {
            Some(value) if value < 1 => Err(DatabaseConcurrencyError::NonPositiveValue { field }),
            Some(value) => u32::try_from(value)
                .map(ConcurrencyValue::Explicit)
                .map_err(|_| invalid()),
            None => Err(invalid()),
        },
        _ => Err(invalid()),
    }
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
    let resolution = resolve_database_concurrency(&to_config(stored)?, capacity, cpu_count)?;
    let native_budget = resolution.native_pool_max_size;
    Ok(PoolBudget {
        resolution,
        native_budget,
        share: POOL_SHARE,
        degraded: (native_budget < NATIVE_POOL_COUNT * POOL_SHARE).then_some("database_pool"),
    })
}

#[cfg(test)]
mod tests;

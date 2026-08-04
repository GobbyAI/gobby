use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const BOOTSTRAP_POOL_SIZE: u32 = 2;
pub const MIN_POOL_SIZE: u32 = 32;
pub const MIN_EXECUTOR_WORKERS: u32 = 8;
pub const MIN_SUPPORTED_CPUS: u32 = 8;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(untagged)]
pub enum ConcurrencyValue {
    Automatic(String),
    Explicit(u32),
}

impl Default for ConcurrencyValue {
    fn default() -> Self {
        Self::Automatic("auto".to_owned())
    }
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
pub struct DatabaseConcurrencyConfig {
    pub pool_max_size: ConcurrencyValue,
    pub executor_max_workers: ConcurrencyValue,
    pub coverage_max_concurrency: ConcurrencyValue,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct PostgresCapacity {
    pub max_connections: u32,
    pub superuser_reserved_connections: u32,
    #[serde(default)]
    pub reserved_connections: u32,
}

impl PostgresCapacity {
    pub fn usable_connections(&self) -> Result<u32, DatabaseConcurrencyError> {
        let reserved = self
            .superuser_reserved_connections
            .checked_add(self.reserved_connections)
            .ok_or(DatabaseConcurrencyError::ReservedConnectionsOverflow)?;
        self.max_connections
            .checked_sub(reserved)
            .ok_or(DatabaseConcurrencyError::ReservedConnectionsExceedMaximum)
    }
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct DatabaseConcurrencyResolution {
    pub cpu_count: u32,
    pub max_connections: u32,
    pub superuser_reserved_connections: u32,
    pub reserved_connections: u32,
    pub usable_connections: u32,
    pub pool_budget: u32,
    pub pool_max_size: u32,
    pub executor_max_workers: u32,
    pub coverage_max_concurrency: u32,
    pub direct_connection_reserve: u32,
    pub bootstrap_pool_size: u32,
    pub hardware_warning: Option<String>,
}

#[derive(Debug, Error)]
pub enum DatabaseConcurrencyError {
    #[error("PostgreSQL reserved connection count overflowed")]
    ReservedConnectionsOverflow,
    #[error("PostgreSQL reserved connections exceed max_connections")]
    ReservedConnectionsExceedMaximum,
    #[error(
        "PostgreSQL usable connection capacity cannot admit the two-connection bootstrap pool (usable={usable})"
    )]
    BootstrapPoolCannotBeAdmitted { usable: u32 },
    #[error(
        "database_concurrency.pool_max_size resolves to {pool}; at least {minimum} connections are required"
    )]
    PoolBelowMinimum { pool: u32, minimum: u32 },
    #[error(
        "database_concurrency.pool_max_size={pool} exceeds the single-daemon budget of {budget} from {usable} usable PostgreSQL connections"
    )]
    PoolExceedsBudget { pool: u32, budget: u32, usable: u32 },
    #[error("database_concurrency.coverage_max_concurrency must be between 1 and 8")]
    CoverageOutOfRange,
    #[error(
        "database_concurrency.executor_max_workers resolves to {workers}; at least {minimum} workers are required"
    )]
    WorkersBelowMinimum { workers: u32, minimum: u32 },
    #[error(
        "database concurrency limits exceed pool capacity: workers={workers}, coverage={coverage}, direct_reserve={direct_reserve}, pool={pool}"
    )]
    LimitsExceedPool {
        workers: u32,
        coverage: u32,
        direct_reserve: u32,
        pool: u32,
    },
    #[error("{field} must be 'auto' or a positive integer, got {value:?}")]
    InvalidAutomaticValue { field: &'static str, value: String },
    #[error("{field} must be a positive integer")]
    NonPositiveValue { field: &'static str },
}

pub fn resolve_database_concurrency(
    config: &DatabaseConcurrencyConfig,
    capacity: &PostgresCapacity,
    cpu_count: u32,
) -> Result<DatabaseConcurrencyResolution, DatabaseConcurrencyError> {
    let cpu_count = cpu_count.max(1);
    let usable = capacity.usable_connections()?;
    if usable < BOOTSTRAP_POOL_SIZE {
        return Err(DatabaseConcurrencyError::BootstrapPoolCannotBeAdmitted { usable });
    }

    let pool_budget = (((u64::from(usable) * 3) / 4) / 8 * 8) as u32;
    let pool = configured_or_auto(
        "database_concurrency.pool_max_size",
        &config.pool_max_size,
        64.min(pool_budget),
    )?;
    if pool < MIN_POOL_SIZE {
        return Err(DatabaseConcurrencyError::PoolBelowMinimum {
            pool,
            minimum: MIN_POOL_SIZE,
        });
    }
    if pool > pool_budget {
        return Err(DatabaseConcurrencyError::PoolExceedsBudget {
            pool,
            budget: pool_budget,
            usable,
        });
    }

    let coverage = configured_or_auto(
        "database_concurrency.coverage_max_concurrency",
        &config.coverage_max_concurrency,
        (cpu_count / 4).clamp(1, 4),
    )?;
    if !(1..=8).contains(&coverage) {
        return Err(DatabaseConcurrencyError::CoverageOutOfRange);
    }

    let direct_reserve = 8.max(pool / 4);
    let available_workers = pool.saturating_sub(coverage.saturating_add(direct_reserve));
    let automatic_workers = cpu_count
        .saturating_mul(2)
        .clamp(MIN_EXECUTOR_WORKERS, 32)
        .min(available_workers);
    let workers = configured_or_auto(
        "database_concurrency.executor_max_workers",
        &config.executor_max_workers,
        automatic_workers,
    )?;
    if workers < MIN_EXECUTOR_WORKERS {
        return Err(DatabaseConcurrencyError::WorkersBelowMinimum {
            workers,
            minimum: MIN_EXECUTOR_WORKERS,
        });
    }
    if workers
        .saturating_add(coverage)
        .saturating_add(direct_reserve)
        > pool
    {
        return Err(DatabaseConcurrencyError::LimitsExceedPool {
            workers,
            coverage,
            direct_reserve,
            pool,
        });
    }

    let hardware_warning = (cpu_count < MIN_SUPPORTED_CPUS).then(|| {
        format!(
            "effective CPU count {cpu_count} is below the supported {MIN_SUPPORTED_CPUS}-CPU baseline; running with degraded database concurrency"
        )
    });
    Ok(DatabaseConcurrencyResolution {
        cpu_count,
        max_connections: capacity.max_connections,
        superuser_reserved_connections: capacity.superuser_reserved_connections,
        reserved_connections: capacity.reserved_connections,
        usable_connections: usable,
        pool_budget,
        pool_max_size: pool,
        executor_max_workers: workers,
        coverage_max_concurrency: coverage,
        direct_connection_reserve: direct_reserve,
        bootstrap_pool_size: BOOTSTRAP_POOL_SIZE,
        hardware_warning,
    })
}

fn configured_or_auto(
    field: &'static str,
    configured: &ConcurrencyValue,
    automatic: u32,
) -> Result<u32, DatabaseConcurrencyError> {
    match configured {
        ConcurrencyValue::Automatic(value) if value == "auto" => Ok(automatic),
        ConcurrencyValue::Automatic(value) => {
            Err(DatabaseConcurrencyError::InvalidAutomaticValue {
                field,
                value: value.clone(),
            })
        }
        ConcurrencyValue::Explicit(value) if *value > 0 => Ok(*value),
        ConcurrencyValue::Explicit(_) => Err(DatabaseConcurrencyError::NonPositiveValue { field }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Debug, Deserialize)]
    struct Contract {
        version: u32,
        cases: Vec<Case>,
    }

    #[derive(Debug, Deserialize)]
    struct Case {
        name: String,
        cpu_count: u32,
        capacity: PostgresCapacity,
        config: DatabaseConcurrencyConfig,
        expected: Option<Expected>,
        error_contains: Option<String>,
    }

    #[derive(Debug, Deserialize)]
    struct Expected {
        usable_connections: u32,
        pool_budget: u32,
        pool_max_size: u32,
        executor_max_workers: u32,
        coverage_max_concurrency: u32,
        direct_connection_reserve: u32,
        bootstrap_pool_size: u32,
        hardware_warning: bool,
    }

    #[test]
    fn shared_sizing_vectors_conform() {
        let contract: Contract = serde_json::from_str(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../docs/contracts/database-concurrency-v1.json"
        )))
        .expect("shared database concurrency contract must parse");
        assert_eq!(contract.version, 1);

        for case in contract.cases {
            let resolved =
                resolve_database_concurrency(&case.config, &case.capacity, case.cpu_count);
            if let Some(expected) = case.expected {
                let resolved = resolved.unwrap_or_else(|error| {
                    panic!("case {} unexpectedly failed: {error}", case.name)
                });
                assert_eq!(
                    resolved.usable_connections, expected.usable_connections,
                    "{}",
                    case.name
                );
                assert_eq!(resolved.pool_budget, expected.pool_budget, "{}", case.name);
                assert_eq!(
                    resolved.pool_max_size, expected.pool_max_size,
                    "{}",
                    case.name
                );
                assert_eq!(
                    resolved.executor_max_workers, expected.executor_max_workers,
                    "{}",
                    case.name
                );
                assert_eq!(
                    resolved.coverage_max_concurrency, expected.coverage_max_concurrency,
                    "{}",
                    case.name
                );
                assert_eq!(
                    resolved.direct_connection_reserve, expected.direct_connection_reserve,
                    "{}",
                    case.name
                );
                assert_eq!(
                    resolved.bootstrap_pool_size, expected.bootstrap_pool_size,
                    "{}",
                    case.name
                );
                assert_eq!(
                    resolved.hardware_warning.is_some(),
                    expected.hardware_warning,
                    "{}",
                    case.name
                );
            } else {
                let error = resolved.expect_err("invalid vector unexpectedly resolved");
                let expected = case
                    .error_contains
                    .expect("invalid vector needs error_contains");
                assert!(
                    error.to_string().contains(&expected),
                    "case {}: {error:?}",
                    case.name
                );
            }
        }
    }
}

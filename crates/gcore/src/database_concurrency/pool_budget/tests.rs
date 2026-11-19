use super::*;

const POOL: &str = "database_concurrency.pool_max_size";
const WORKERS: &str = "database_concurrency.executor_max_workers";
const COVERAGE: &str = "database_concurrency.coverage_max_concurrency";

/// 100 connections with 3 reserved: 97 usable, a pool budget of 72.
fn capacity(max_connections: u32) -> PostgresCapacity {
    PostgresCapacity {
        max_connections,
        superuser_reserved_connections: 3,
        reserved_connections: 0,
    }
}

fn keys(pool: Option<&str>, workers: Option<&str>, coverage: Option<&str>) -> StoredKeys {
    StoredKeys {
        pool_max_size: pool.map(str::to_owned),
        executor_max_workers: workers.map(str::to_owned),
        coverage_max_concurrency: coverage.map(str::to_owned),
    }
}

fn resolve(
    stored: StoredKeys,
    max_connections: u32,
) -> Result<PoolBudget, DatabaseConcurrencyError> {
    resolve_pool_budget(&stored, &capacity(max_connections), 8)
}

#[test]
fn each_key_answers_or_refuses() {
    let automatic = resolve(StoredKeys::default(), 100).expect("automatic keys resolve");
    assert_eq!(automatic.resolution.pool_max_size, 64);
    assert_eq!(automatic.native_budget, 8);

    assert!(matches!(
        resolve(keys(Some("31"), None, None), 100),
        Err(DatabaseConcurrencyError::PoolBelowMinimum {
            pool: 31,
            minimum: 32
        })
    ));
    assert!(matches!(
        resolve(keys(Some("80"), None, None), 100),
        Err(DatabaseConcurrencyError::PoolExceedsBudget {
            pool: 80,
            budget: 72,
            usable: 97
        })
    ));
    assert!(matches!(
        resolve(keys(None, None, Some("9")), 100),
        Err(DatabaseConcurrencyError::CoverageOutOfRange)
    ));
    assert!(matches!(
        resolve(keys(None, None, Some("0")), 100),
        Err(DatabaseConcurrencyError::NonPositiveValue { field: COVERAGE })
    ));
    assert!(matches!(
        resolve(keys(None, Some("7"), None), 100),
        Err(DatabaseConcurrencyError::WorkersBelowMinimum {
            workers: 7,
            minimum: 8
        })
    ));
    assert!(matches!(
        resolve(keys(Some("64"), Some("50"), Some("4")), 100),
        Err(DatabaseConcurrencyError::LimitsExceedPool {
            workers: 50,
            coverage: 4,
            direct_reserve: 16,
            pool: 64
        })
    ));
    assert!(matches!(
        resolve(keys(None, Some("\"max\""), None), 100),
        Err(DatabaseConcurrencyError::InvalidStoredValue { field: WORKERS })
    ));
    assert!(matches!(
        resolve(StoredKeys::default(), 4),
        Err(DatabaseConcurrencyError::BootstrapPoolCannotBeAdmitted { usable: 1 })
    ));

    let small = resolve(StoredKeys::default(), 50).expect("50 connections resolve");
    assert_eq!(small.native_budget, 0);
    let medium = resolve(StoredKeys::default(), 80).expect("80 connections resolve");
    assert_eq!(medium.native_budget, 0);
}

#[test]
fn stored_values_are_strict_json() {
    assert_eq!(
        parse_stored(POOL, None).expect("absent is automatic"),
        ConcurrencyValue::Automatic("auto".into())
    );
    assert_eq!(
        parse_stored(POOL, Some("\"auto\"")).expect("the auto string is automatic"),
        ConcurrencyValue::Automatic("auto".into())
    );
    assert_eq!(
        parse_stored(POOL, Some("72")).expect("an integer is explicit"),
        ConcurrencyValue::Explicit(72)
    );
    assert!(matches!(
        parse_stored(POOL, Some("\"72\"")),
        Err(DatabaseConcurrencyError::InvalidStoredValue { field: POOL })
    ));
    assert!(matches!(
        parse_stored(POOL, Some("0")),
        Err(DatabaseConcurrencyError::NonPositiveValue { field: POOL })
    ));
    for raw in ["-1", "true", "1.5", "null", "\"max\"", "auto", ""] {
        assert!(
            parse_stored(POOL, Some(raw)).is_err(),
            "stored text {raw:?} must refuse"
        );
    }
}

#[test]
fn four_pools_share_the_native_budget() {
    let full = resolve(StoredKeys::default(), 100).expect("a full native budget resolves");
    assert_eq!(full.native_budget, 8);
    assert_eq!(full.share, POOL_SHARE);
    assert_eq!(4 * full.share, full.native_budget);
    assert_eq!(full.degraded, None);

    let short = resolve(keys(Some("66"), None, None), 100).expect("a short native budget resolves");
    assert_eq!(short.native_budget, 6);
    assert_eq!(short.share, POOL_SHARE);
    assert_eq!(short.degraded, Some("database_pool"));

    let empty = resolve(StoredKeys::default(), 50).expect("an empty native budget resolves");
    assert_eq!(empty.native_budget, 0);
    assert_eq!(empty.degraded, Some("database_pool"));
}

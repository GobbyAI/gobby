use anyhow::Context;
use postgres::Client;

use crate::schema;

mod communities;
mod ids;
mod local_callee;
mod pin_gaps;
mod queries;
mod resolution;

pub use communities::*;
pub use ids::*;
pub use local_callee::*;
pub use pin_gaps::*;
pub use queries::*;
pub use resolution::*;

/// Open a connection for command paths that may write to the hub.
///
/// This currently shares the same connection logic as read-only callers, but
/// keeping the intent explicit preserves a routing point for future pools,
/// permissions, or replicas.
pub fn connect_readwrite(database_url: &str) -> anyhow::Result<Client> {
    let mut client = gobby_core::postgres::connect_readwrite(database_url)?;
    // pg_search's parallel BM25 scans can segfault a worker and restart the hub.
    // Keep the index enabled, but execute searches in this connection's leader.
    client.batch_execute("SET max_parallel_workers_per_gather = 0")?;
    schema::validate_runtime_schema(&mut client)?;
    Ok(client)
}

/// Open the connection for freshness refreshes, which run under a try-lock and
/// must not wait on a row another session holds. Both bounds are session-level
/// and scoped to this connection: a lock wait past the timeout fails the refresh
/// (55P03) and a runaway statement is cancelled (57014).
pub fn connect_freshness_readwrite(database_url: &str) -> anyhow::Result<Client> {
    let mut client = connect_readwrite(database_url)?;
    client
        .batch_execute("SET lock_timeout = '5s'; SET statement_timeout = '60s'")
        .context("failed to bound the freshness refresh connection")?;
    Ok(client)
}

/// Open a connection for command paths that only read from the hub.
///
/// This currently shares the same connection logic as read-write callers, but
/// keeping the intent explicit preserves a routing point for future pools,
/// permissions, or replicas.
pub fn connect_readonly(database_url: &str) -> anyhow::Result<Client> {
    let mut client = gobby_core::postgres::connect_readonly(database_url)?;
    client.batch_execute("SET max_parallel_workers_per_gather = 0")?;
    schema::validate_runtime_schema(&mut client)?;
    Ok(client)
}

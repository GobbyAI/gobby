use postgres::Client;

use crate::schema;

mod queries;
mod resolution;

pub use queries::*;
pub use resolution::*;

/// Open a connection for command paths that may write to the hub.
///
/// This currently shares the same connection logic as read-only callers, but
/// keeping the intent explicit preserves a routing point for future pools,
/// permissions, or replicas.
pub fn connect_readwrite(database_url: &str) -> anyhow::Result<Client> {
    let mut client = gobby_core::postgres::connect_readwrite(database_url)?;
    schema::validate_runtime_schema(&mut client)?;
    Ok(client)
}

/// Open a connection for command paths that only read from the hub.
///
/// This currently shares the same connection logic as read-write callers, but
/// keeping the intent explicit preserves a routing point for future pools,
/// permissions, or replicas.
pub fn connect_readonly(database_url: &str) -> anyhow::Result<Client> {
    let mut client = gobby_core::postgres::connect_readonly(database_url)?;
    schema::validate_runtime_schema(&mut client)?;
    Ok(client)
}

pub fn read_config_value(conn: &mut Client, key: &str) -> anyhow::Result<Option<String>> {
    gobby_core::postgres::read_config_value(conn, key)
}

use postgres::Client;

use crate::{WikiError, support::env};

pub(crate) fn require_postgres_index(command: &str) -> Result<Client, WikiError> {
    let database_url = env::database_url_for(command)?.ok_or_else(|| WikiError::Config {
        detail: format!(
            "PostgreSQL index is required for {command}; configure GWIKI_DATABASE_URL or GOBBY_POSTGRES_DSN"
        ),
    })?;

    gobby_core::postgres::connect_readonly(&database_url).map_err(|error| WikiError::Config {
        detail: format!("failed to connect to PostgreSQL for {command}: {error}"),
    })
}

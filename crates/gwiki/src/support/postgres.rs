use gobby_core::schema::ValidationContext;
use postgres::Client;

use crate::{WikiError, support::env};

pub(crate) fn require_attached_index(command: &'static str) -> Result<(), WikiError> {
    require_attached_index_from_database_url(command, env::database_url_for(command)?)
}

fn require_attached_index_from_database_url(
    command: &'static str,
    database_url: Option<String>,
) -> Result<(), WikiError> {
    let Some(database_url) = database_url else {
        return Err(WikiError::Config {
            detail: format!(
                "{command}: PostgreSQL index is required but no PostgreSQL hub is configured"
            ),
        });
    };

    let mut conn = gobby_core::postgres::connect_readonly(&database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for {command}: {error}"),
        }
    })?;
    let mut ctx = ValidationContext {
        pg: Some(&mut conn),
        falkor_config: None,
        qdrant_config: None,
    };
    let report = crate::schema::validate_runtime_schema(&mut ctx);
    if report.missing.is_empty() {
        return Ok(());
    }

    let missing = report
        .missing
        .into_iter()
        .map(|(name, issue)| format!("{name}: {}", issue.guidance.problem))
        .collect::<Vec<_>>()
        .join("; ");
    Err(WikiError::Config {
        detail: format!("{command}: PostgreSQL index is required but validation failed: {missing}"),
    })
}

#[cfg(test)]
pub(crate) fn require_attached_index_without_database_for_test(
    command: &'static str,
) -> Result<(), WikiError> {
    require_attached_index_from_database_url(command, None)
}

pub(crate) fn require_postgres_index(command: &'static str) -> Result<Client, WikiError> {
    let database_url = env::database_url_for(command)?.ok_or_else(|| WikiError::Config {
        detail: format!("PostgreSQL index is required for {command}; acquire a runtime grant"),
    })?;

    gobby_core::postgres::connect_readonly(&database_url).map_err(|error| WikiError::Config {
        detail: format!("failed to connect to PostgreSQL for {command}: {error}"),
    })
}

pub(crate) fn require_postgres_index_readwrite(command: &'static str) -> Result<Client, WikiError> {
    let database_url = env::database_url_for(command)?.ok_or_else(|| WikiError::Config {
        detail: format!("PostgreSQL index is required for {command}; acquire a runtime grant"),
    })?;

    gobby_core::postgres::connect_readwrite(&database_url).map_err(|error| WikiError::Config {
        detail: format!("failed to connect to PostgreSQL for {command}: {error}"),
    })
}

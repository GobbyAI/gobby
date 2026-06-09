use gobby_core::setup::ValidationContext;
use postgres::Client;

use crate::{WikiError, support::env};

pub(crate) fn require_attached_index(command: &'static str) -> Result<(), WikiError> {
    let Some(database_url) = env::database_url_for(command)? else {
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

pub(crate) fn require_postgres_index(command: &'static str) -> Result<Client, WikiError> {
    let database_url = env::database_url_for(command)?.ok_or_else(|| WikiError::Config {
        detail: format!(
            "PostgreSQL index is required for {command}; configure GWIKI_DATABASE_URL or GOBBY_POSTGRES_DSN"
        ),
    })?;

    gobby_core::postgres::connect_readonly(&database_url).map_err(|error| WikiError::Config {
        detail: format!("failed to connect to PostgreSQL for {command}: {error}"),
    })
}

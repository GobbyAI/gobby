use anyhow::Context as _;
use postgres::{Client, NoTls};

use crate::db;
use crate::output::{self, Format};
use crate::setup::{self, StandaloneSetupRequest};

pub fn run(request: StandaloneSetupRequest, format: Format, quiet: bool) -> anyhow::Result<()> {
    let database_url = request
        .database_url
        .as_deref()
        .map(str::to_string)
        .map(Ok)
        .unwrap_or_else(db::resolve_database_url)?;
    let mut client = Client::connect(&database_url, NoTls)
        .context("failed to connect to the standalone PostgreSQL database")?;
    let status = setup::run_standalone_setup(&request, &mut client)?;

    match format {
        Format::Json => output::print_json(&status),
        Format::Text => {
            if !quiet {
                output::print_text(&format!(
                    "Standalone gcode setup complete in schema {}",
                    status.schema
                ))?;
            }
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn standalone_command_is_scoped() {
        let Ok(database_url) = std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL") else {
            return;
        };
        let schema = format!("gcode_setup_test_{}", std::process::id());
        let request = StandaloneSetupRequest {
            standalone: true,
            database_url: Some(database_url.clone()),
            schema: schema.clone(),
        };

        run(request, Format::Json, true).expect("standalone setup runs");

        let mut client =
            postgres::Client::connect(&database_url, postgres::NoTls).expect("connect test db");
        let table_name = format!("{schema}.code_symbols");
        let exists: bool = client
            .query_one("SELECT to_regclass($1) IS NOT NULL", &[&table_name])
            .expect("check code_symbols")
            .get(0);
        assert!(exists);

        let forbidden = format!("{schema}.config_store");
        let forbidden_exists: bool = client
            .query_one("SELECT to_regclass($1) IS NOT NULL", &[&forbidden])
            .expect("check config_store")
            .get(0);
        assert!(!forbidden_exists);

        client
            .batch_execute(&format!("DROP SCHEMA IF EXISTS {schema} CASCADE"))
            .expect("drop test schema");
    }
}

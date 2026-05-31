//! PostgreSQL foundation adapter boundary and hub connection helpers.
//!
//! This module is available with the `postgres` feature. Gobby-owned schemas are
//! externally managed; adapter code must validate required objects without
//! creating, altering, or dropping them. This module is intentionally
//! schema-agnostic; consumers supply any table or index validation.

use anyhow::Context;
use postgres::{Client, NoTls, config::SslMode, error::SqlState};
use postgres_native_tls::MakeTlsConnector;

/// Connect to the PostgreSQL hub in read-only mode.
///
/// Sets `default_transaction_read_only = on` to guard against accidental writes.
pub fn connect_readonly(database_url: &str) -> anyhow::Result<Client> {
    let mut client = connect(database_url)?;
    client
        .execute("SET default_transaction_read_only = on", &[])
        .context("failed to set PostgreSQL connection read-only")?;
    Ok(client)
}

/// Connect to the PostgreSQL hub with write access.
pub fn connect_readwrite(database_url: &str) -> anyhow::Result<Client> {
    connect(database_url)
}

/// Read a raw config value from the Gobby `config_store` table.
///
/// Returns the raw stored value (which may be JSON-encoded). Callers should
/// decode JSON string encoding and resolve `$secret:NAME` or `${VAR}` values
/// in their own config layer.
///
/// Returns `None` for missing keys. Does not write.
pub fn read_config_value(conn: &mut Client, key: &str) -> anyhow::Result<Option<String>> {
    let row = conn
        .query_opt("SELECT value FROM config_store WHERE key = $1", &[&key])
        .with_context(|| format!("failed to read config_store key {key:?}"))?;
    row.map(|r| {
        r.try_get("value")
            .with_context(|| format!("config_store key {key:?} value was not text"))
    })
    .transpose()
}

/// Result of a single schema object check (table, index, column, etc.).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SchemaCheck {
    /// Object name (for example, `symbols` or `bm25_symbols_idx`).
    pub object_name: String,
    /// What was checked (for example, `table exists` or `column type`).
    pub check_kind: String,
    /// Whether the check passed.
    pub passed: bool,
    /// Detail on failure.
    pub detail: Option<String>,
}

/// Run a consumer-supplied schema validator for attached-mode checks.
///
/// The callback receives a mutable connection because `postgres::Client`
/// query methods require `&mut self`. `gobby-core` does not know which tables
/// to check and never runs migrations.
pub fn validate_schema(
    conn: &mut Client,
    validator: impl FnOnce(&mut Client) -> Vec<SchemaCheck>,
) -> Vec<SchemaCheck> {
    run_schema_validator(conn, validator)
}

fn connect(database_url: &str) -> anyhow::Result<Client> {
    let config = database_url
        .parse::<postgres::Config>()
        .context("failed to parse PostgreSQL connection URL")?;
    match config.get_ssl_mode() {
        SslMode::Disable => config
            .connect(NoTls)
            .context("failed to connect to the Gobby PostgreSQL hub"),
        SslMode::Prefer => match connect_with_tls_unverified(&config) {
            Ok(client) => Ok(client),
            Err(error) if is_no_tls_server_error(&error) => config
                .connect(NoTls)
                .context("failed to connect to the Gobby PostgreSQL hub"),
            Err(error) => Err(error),
        },
        // libpq `sslmode=require` requires encryption without CA or hostname
        // verification. `verify-ca` and `verify-full` keep verification enabled.
        SslMode::Require => connect_with_tls_unverified(&config),
        _ => connect_with_tls_verification(&config, true),
    }
}

fn connect_with_tls_unverified(config: &postgres::Config) -> anyhow::Result<Client> {
    connect_with_tls_verification(config, false)
}

fn connect_with_tls_verification(
    config: &postgres::Config,
    verify: bool,
) -> anyhow::Result<Client> {
    let mut builder = native_tls::TlsConnector::builder();
    if !verify {
        builder.danger_accept_invalid_certs(true);
        builder.danger_accept_invalid_hostnames(true);
    }
    let connector = builder
        .build()
        .context("failed to build PostgreSQL TLS connector")?;
    config
        .connect(MakeTlsConnector::new(connector))
        .context("failed to connect to the Gobby PostgreSQL hub")
}

fn is_no_tls_server_error(error: &anyhow::Error) -> bool {
    error.chain().any(is_no_tls_postgres_error)
        || error.chain().any(is_no_tls_native_tls_error)
        || error.chain().any(is_no_tls_error_message)
}

fn is_no_tls_postgres_error(error: &(dyn std::error::Error + 'static)) -> bool {
    error
        .downcast_ref::<postgres::Error>()
        .and_then(postgres::Error::as_db_error)
        .is_some_and(|db_error| {
            matches!(
                *db_error.code(),
                SqlState::INVALID_PARAMETER_VALUE | SqlState::CONNECTION_FAILURE
            ) && is_no_tls_error_message(error)
        })
}

fn is_no_tls_native_tls_error(error: &(dyn std::error::Error + 'static)) -> bool {
    error
        .downcast_ref::<native_tls::Error>()
        .is_some_and(|_| is_no_tls_error_message(error))
}

fn is_no_tls_error_message(error: &(dyn std::error::Error + 'static)) -> bool {
    let message = error.to_string().to_ascii_lowercase();
    message.contains("server does not support tls")
        || message.contains("the server does not support ssl")
        || message.contains("tls not supported")
        || message.contains("ssl is not enabled")
}

fn run_schema_validator<C>(
    conn: &mut C,
    validator: impl FnOnce(&mut C) -> Vec<SchemaCheck>,
) -> Vec<SchemaCheck> {
    validator(conn)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn attached_validation_is_non_destructive() {
        let mut conn = vec!["existing-state"];

        let checks = run_schema_validator(&mut conn, |conn| {
            assert_eq!(conn.as_slice(), ["existing-state"]);
            conn.push("validator-ran");
            vec![SchemaCheck {
                object_name: "consumer_table".to_string(),
                check_kind: "table exists".to_string(),
                passed: true,
                detail: None,
            }]
        });

        assert_eq!(conn, vec!["existing-state", "validator-ran"]);
        assert_eq!(checks.len(), 1);
        assert_eq!(checks[0].object_name, "consumer_table");
        assert!(checks[0].passed);
    }

    #[test]
    fn schema_validator_is_domain_supplied() {
        let mut domain_objects = ["domain_symbols", "domain_bm25_idx"].into_iter();

        let checks = run_schema_validator(&mut domain_objects, |objects| {
            objects
                .map(|object_name| SchemaCheck {
                    object_name: object_name.to_string(),
                    check_kind: "consumer supplied".to_string(),
                    passed: true,
                    detail: None,
                })
                .collect::<Vec<_>>()
        });

        assert_eq!(
            checks
                .iter()
                .map(|check| check.object_name.as_str())
                .collect::<Vec<_>>(),
            vec!["domain_symbols", "domain_bm25_idx"]
        );
    }

    #[test]
    fn validate_schema_accepts_postgres_client_validators() {
        type ClientSchemaValidator = fn(&mut Client) -> Vec<SchemaCheck>;
        type ValidateSchema = fn(&mut Client, ClientSchemaValidator) -> Vec<SchemaCheck>;

        let _validate: ValidateSchema = validate_schema;
    }

    #[test]
    fn sslmode_parser_selects_tls_modes() {
        let require = "postgresql://user:pass@localhost/db?sslmode=require"
            .parse::<postgres::Config>()
            .expect("parse require");
        let disable = "postgresql://user:pass@localhost/db?sslmode=disable"
            .parse::<postgres::Config>()
            .expect("parse disable");

        assert_eq!(require.get_ssl_mode(), SslMode::Require);
        assert_eq!(disable.get_ssl_mode(), SslMode::Disable);
    }

    #[test]
    fn prefer_mode_fallback_is_limited_to_no_tls_server_errors() {
        assert!(is_no_tls_server_error(&anyhow::anyhow!(
            "server does not support TLS"
        )));
        assert!(!is_no_tls_server_error(&anyhow::anyhow!(
            "certificate verify failed"
        )));
    }
}

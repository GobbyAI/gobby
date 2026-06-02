//! PostgreSQL foundation adapter boundary and hub connection helpers.
//!
//! This module is available with the `postgres` feature. Gobby-owned schemas are
//! externally managed; adapter code must validate required objects without
//! creating, altering, or dropping them. This module is intentionally
//! schema-agnostic; consumers supply any table or index validation.

use anyhow::Context;
use postgres::{Client, NoTls, config::SslMode};
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
    let requested_ssl_mode = requested_ssl_mode(database_url);
    let normalized_url = normalize_sslmode_for_parser(database_url);
    let config = normalized_url
        .parse::<postgres::Config>()
        .context("failed to parse PostgreSQL connection URL")?;
    match requested_ssl_mode.unwrap_or_else(|| requested_ssl_mode_from_config(&config)) {
        RequestedSslMode::Disable => config
            .connect(NoTls)
            .context("failed to connect to the Gobby PostgreSQL hub"),
        RequestedSslMode::Prefer => match connect_with_tls_unverified(&config) {
            Ok(client) => Ok(client),
            Err(error) => {
                log::debug!(
                    "PostgreSQL sslmode=prefer TLS attempt failed; retrying without TLS: {error}"
                );
                config
                    .connect(NoTls)
                    .context("failed to connect to the Gobby PostgreSQL hub")
            }
        },
        // libpq `sslmode=require` requires encryption without CA or hostname
        // verification. `verify-ca` keeps CA verification while allowing
        // hostname mismatch; `verify-full` keeps both checks strict.
        RequestedSslMode::Require => connect_with_tls_unverified(&config),
        RequestedSslMode::VerifyCa => connect_with_tls_verify_ca(&config),
        RequestedSslMode::VerifyFull => connect_with_tls_verification(&config, true),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RequestedSslMode {
    Disable,
    Prefer,
    Require,
    VerifyCa,
    VerifyFull,
}

fn requested_ssl_mode_from_config(config: &postgres::Config) -> RequestedSslMode {
    match config.get_ssl_mode() {
        SslMode::Disable => RequestedSslMode::Disable,
        SslMode::Prefer => RequestedSslMode::Prefer,
        SslMode::Require => RequestedSslMode::Require,
        _ => RequestedSslMode::Prefer,
    }
}

fn requested_ssl_mode(database_url: &str) -> Option<RequestedSslMode> {
    let value = sslmode_value(database_url)?;
    match value.as_str() {
        "disable" => Some(RequestedSslMode::Disable),
        "prefer" => Some(RequestedSslMode::Prefer),
        "require" => Some(RequestedSslMode::Require),
        "verify-ca" => Some(RequestedSslMode::VerifyCa),
        "verify-full" => Some(RequestedSslMode::VerifyFull),
        _ => {
            log::debug!("unrecognized PostgreSQL sslmode value `{value}`; using parser default");
            None
        }
    }
}

fn sslmode_value(database_url: &str) -> Option<String> {
    if let Some((_, query)) = database_url.split_once('?') {
        return query.split('&').find_map(|pair| {
            let (key, value) = pair.split_once('=')?;
            (key == "sslmode").then(|| normalize_sslmode_token(value))
        });
    }

    database_url.split_whitespace().find_map(|part| {
        let (key, value) = part.split_once('=')?;
        (key == "sslmode").then(|| normalize_sslmode_token(value))
    })
}

fn normalize_sslmode_for_parser(database_url: &str) -> String {
    if let Some((base, query)) = database_url.split_once('?') {
        let query = query
            .split('&')
            .map(normalize_sslmode_pair)
            .collect::<Vec<_>>()
            .join("&");
        return format!("{base}?{query}");
    }

    database_url
        .split_whitespace()
        .map(normalize_sslmode_pair)
        .collect::<Vec<_>>()
        .join(" ")
}

fn normalize_sslmode_pair(pair: &str) -> String {
    let Some((key, value)) = pair.split_once('=') else {
        return pair.to_string();
    };
    if key != "sslmode" {
        return pair.to_string();
    }
    let token = normalize_sslmode_token(value);
    if matches!(token.as_str(), "verify-ca" | "verify-full") {
        "sslmode=require".to_string()
    } else {
        format!("sslmode={token}")
    }
}

fn normalize_sslmode_token(value: &str) -> String {
    value
        .trim_matches('\'')
        .trim_matches('"')
        .to_ascii_lowercase()
}

fn connect_with_tls_unverified(config: &postgres::Config) -> anyhow::Result<Client> {
    connect_with_tls_verification(config, false)
}

fn connect_with_tls_verify_ca(config: &postgres::Config) -> anyhow::Result<Client> {
    let mut builder = native_tls::TlsConnector::builder();
    builder.danger_accept_invalid_hostnames(true);
    let connector = builder
        .build()
        .context("failed to build PostgreSQL TLS connector")?;
    config
        .connect(MakeTlsConnector::new(connector))
        .context("failed to connect to the Gobby PostgreSQL hub")
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
        assert!(std::ptr::fn_addr_eq(
            _validate,
            validate_schema as ValidateSchema
        ));
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
    fn quoted_verify_sslmodes_normalize_for_postgres_parser() {
        assert_eq!(
            requested_ssl_mode("postgresql://localhost/db?sslmode='verify-full'"),
            Some(RequestedSslMode::VerifyFull)
        );
        assert_eq!(
            normalize_sslmode_for_parser("postgresql://localhost/db?sslmode='prefer'&x=1"),
            "postgresql://localhost/db?sslmode=prefer&x=1"
        );
        assert_eq!(
            normalize_sslmode_for_parser("postgresql://localhost/db?sslmode='verify-ca'&x=1"),
            "postgresql://localhost/db?sslmode=require&x=1"
        );
        assert_eq!(
            normalize_sslmode_for_parser("host=localhost sslmode='prefer' dbname=gobby"),
            "host=localhost sslmode=prefer dbname=gobby"
        );
        assert_eq!(
            normalize_sslmode_for_parser("host=localhost sslmode='verify-full' dbname=gobby"),
            "host=localhost sslmode=require dbname=gobby"
        );
    }
}

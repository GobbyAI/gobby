//! PostgreSQL foundation adapter boundary and hub connection helpers.
//!
//! This module is available with the `postgres` feature. Gobby-owned schemas are
//! externally managed; adapter code must validate required objects without
//! creating, altering, or dropping them. This module is intentionally
//! schema-agnostic; consumers supply any table or index validation.

use anyhow::Context;
use openssl::ssl::{SslConnector, SslConnectorBuilder, SslMethod, SslVerifyMode};
use postgres::{Client, NoTls, config::SslMode};
use postgres_openssl::MakeTlsConnector;

const GOBBY_APPLICATION_NAME: &str = "gobby-cli";
const MANAGED_APPLICATION_NAME_PREFIX: &str = "gobby-agent-";

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
/// decode JSON string encoding and resolve `unresolved secret marker` or `${VAR}` values
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

/// Read the committed global runtime configuration revision.
pub fn read_config_revision(conn: &mut Client) -> anyhow::Result<i64> {
    conn.query_one("SELECT revision FROM config_state WHERE id = true", &[])
        .context("failed to read runtime configuration revision")?
        .try_get("revision")
        .context("runtime configuration revision was not an integer")
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
/// to check and never runs migrations. Validators have the signature
/// `FnOnce(&mut Client) -> Vec<SchemaCheck>`.
pub fn validate_schema(
    conn: &mut Client,
    validator: impl FnOnce(&mut Client) -> Vec<SchemaCheck>,
) -> Vec<SchemaCheck> {
    run_schema_validator(conn, validator)
}

/// Return whether a DSN names a daemon-issued, project-scoped agent principal.
///
/// Both the login role and application name encode the same managed execution
/// UUID. Requiring both prevents an operator DSN with a caller-selected
/// application name from being mistaken for an RLS-scoped connection.
pub fn is_managed_agent_connection(database_url: &str) -> bool {
    let Ok(config) = connection_config(database_url) else {
        return false;
    };
    let Some(execution_id) = config
        .get_application_name()
        .and_then(|name| name.strip_prefix(MANAGED_APPLICATION_NAME_PREFIX))
        .and_then(|value| uuid::Uuid::parse_str(value).ok())
    else {
        return false;
    };
    let Some(role_suffix) = config
        .get_user()
        .and_then(|name| name.strip_prefix("gobby_agent_"))
    else {
        return false;
    };
    let Some((compact_id, generation)) = role_suffix.rsplit_once('_') else {
        return false;
    };
    generation.parse::<u32>().is_ok_and(|value| value > 0)
        && compact_id == execution_id.simple().to_string()
}

fn connection_config(database_url: &str) -> anyhow::Result<postgres::Config> {
    let normalized_url = normalize_sslmode_for_parser(database_url);
    let mut config = normalized_url
        .parse::<postgres::Config>()
        .context("failed to parse PostgreSQL connection URL")?;
    let managed_name = config
        .get_application_name()
        .and_then(|name| name.strip_prefix(MANAGED_APPLICATION_NAME_PREFIX))
        .is_some_and(|execution_id| uuid::Uuid::parse_str(execution_id).is_ok());
    if !managed_name {
        config.application_name(GOBBY_APPLICATION_NAME);
    }
    Ok(config)
}

fn is_password_authentication_failure(error: &anyhow::Error) -> bool {
    error.chain().any(|source| {
        source
            .downcast_ref::<postgres::Error>()
            .and_then(postgres::Error::as_db_error)
            .is_some_and(|db_error| db_error.code() == &postgres::error::SqlState::INVALID_PASSWORD)
    })
}

fn connect_after_grant_rehandshake(original: anyhow::Error) -> anyhow::Result<Client> {
    let cwd = match std::env::current_dir() {
        Ok(cwd) => cwd,
        Err(_) => return Err(original),
    };
    let Some(root) = crate::project::find_project_root(&cwd) else {
        return Err(original);
    };
    let Ok(acquired) =
        crate::grant::rehandshake(&crate::grant::AcquireRequest::from_process(&root))
    else {
        return Err(original);
    };
    let crate::grant::PostgresCapability::Direct { dsn, .. } =
        &acquired.bundle.capabilities.postgres
    else {
        return Err(original);
    };
    connect_once(dsn)
}

fn connect(database_url: &str) -> anyhow::Result<Client> {
    match connect_once(database_url) {
        Ok(client) => Ok(client),
        Err(error) if is_password_authentication_failure(&error) => {
            connect_after_grant_rehandshake(error)
        }
        Err(error) => Err(error),
    }
}

fn connect_once(database_url: &str) -> anyhow::Result<Client> {
    let requested_ssl_mode = requested_ssl_mode(database_url);
    let config = connection_config(database_url)?;
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

    crate::libpq::split_keyword_dsn_tokens(database_url)
        .into_iter()
        .find_map(|part| {
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

    crate::libpq::split_keyword_dsn_tokens(database_url)
        .into_iter()
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
    connect_with_tls(config, TlsConnectorMode::Unverified)
}

fn connect_with_tls_verify_ca(config: &postgres::Config) -> anyhow::Result<Client> {
    connect_with_tls(config, TlsConnectorMode::VerifyCa)
}

fn connect_with_tls_verification(
    config: &postgres::Config,
    verify: bool,
) -> anyhow::Result<Client> {
    let mode = if verify {
        TlsConnectorMode::VerifyFull
    } else {
        TlsConnectorMode::Unverified
    };
    connect_with_tls(config, mode)
}

fn connect_with_tls(config: &postgres::Config, mode: TlsConnectorMode) -> anyhow::Result<Client> {
    let connector = tls_connector(mode)?;
    config
        .connect(connector)
        .context("failed to connect to the Gobby PostgreSQL hub")
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TlsConnectorMode {
    Unverified,
    VerifyCa,
    VerifyFull,
}

impl TlsConnectorMode {
    fn verify_mode(self) -> SslVerifyMode {
        match self {
            Self::Unverified => SslVerifyMode::NONE,
            Self::VerifyCa | Self::VerifyFull => SslVerifyMode::PEER,
        }
    }

    fn uses_default_verify_paths(self) -> bool {
        matches!(self, Self::VerifyCa | Self::VerifyFull)
    }

    fn disables_hostname_verification(self) -> bool {
        matches!(self, Self::VerifyCa)
    }
}

struct TlsConnectorBuilder {
    builder: SslConnectorBuilder,
    #[cfg(test)]
    verify_mode: SslVerifyMode,
    disables_hostname_verification: bool,
}

fn tls_connector(mode: TlsConnectorMode) -> anyhow::Result<MakeTlsConnector> {
    let builder = tls_connector_builder(mode)?;
    let disables_hostname_verification = builder.disables_hostname_verification;
    let mut connector = MakeTlsConnector::new(builder.builder.build());
    if disables_hostname_verification {
        connector.set_callback(|config, _domain| {
            config.set_verify_hostname(false);
            Ok(())
        });
    }
    Ok(connector)
}

fn tls_connector_builder(mode: TlsConnectorMode) -> anyhow::Result<TlsConnectorBuilder> {
    let mut builder = SslConnector::builder(SslMethod::tls())
        .context("failed to build PostgreSQL TLS connector")?;
    if mode.uses_default_verify_paths() {
        builder
            .set_default_verify_paths()
            .context("failed to load PostgreSQL TLS default verify paths")?;
    }
    let verify_mode = mode.verify_mode();
    builder.set_verify(verify_mode);
    Ok(TlsConnectorBuilder {
        builder,
        #[cfg(test)]
        verify_mode,
        disables_hostname_verification: mode.disables_hostname_verification(),
    })
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
    fn password_authentication_failure_requires_sqlstate() {
        let error = anyhow::anyhow!("password authentication failed for user \"gobby\"");
        assert!(!is_password_authentication_failure(&error));
    }

    #[test]
    fn connection_config_enforces_gobby_application_name() -> anyhow::Result<()> {
        let config =
            connection_config("postgresql://localhost/gobby?application_name=operator-supplied")?;

        assert_eq!(config.get_application_name(), Some(GOBBY_APPLICATION_NAME));
        Ok(())
    }

    #[test]
    fn connection_config_preserves_managed_agent_application_name() -> anyhow::Result<()> {
        let managed = "gobby-agent-a158f699-2c1d-44d8-b438-633d7ad8f8db";
        let config = connection_config(&format!(
            "postgresql://localhost/gobby?application_name={managed}"
        ))?;

        assert_eq!(config.get_application_name(), Some(managed));
        Ok(())
    }

    #[test]
    fn managed_agent_connection_requires_matching_role_and_application_identity() {
        let execution_id = "11111111-1111-4111-8111-111111112003";
        let compact_id = "11111111111141118111111111112003";
        let valid = format!(
            "postgresql://gobby_agent_{compact_id}_2:secret@localhost/gobby?\
             application_name=gobby-agent-{execution_id}"
        );
        assert!(is_managed_agent_connection(&valid));

        let operator = format!(
            "postgresql://gobby:secret@localhost/gobby?application_name=gobby-agent-{execution_id}"
        );
        assert!(!is_managed_agent_connection(&operator));

        let mismatched = format!(
            "postgresql://gobby_agent_{compact_id}_2:secret@localhost/gobby?\
             application_name=gobby-agent-22222222-2222-4222-8222-222222222222"
        );
        assert!(!is_managed_agent_connection(&mismatched));

        let missing_application =
            format!("postgresql://gobby_agent_{compact_id}_2:secret@localhost/gobby");
        assert!(!is_managed_agent_connection(&missing_application));
    }

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
        assert_eq!(
            normalize_sslmode_for_parser(
                "host=localhost password='my pass' sslmode='verify-ca' dbname=gobby"
            ),
            "host=localhost password='my pass' sslmode=require dbname=gobby"
        );
        assert_eq!(
            requested_ssl_mode("host=localhost password='my pass' sslmode='verify-full'"),
            Some(RequestedSslMode::VerifyFull)
        );
    }

    #[test]
    fn tls_connector_construction_unverified_disables_peer_verification() -> anyhow::Result<()> {
        let builder = tls_connector_builder(TlsConnectorMode::Unverified)?;
        assert_eq!(builder.verify_mode, SslVerifyMode::NONE);
        assert!(!builder.disables_hostname_verification);

        let _connector = tls_connector(TlsConnectorMode::Unverified)?;
        Ok(())
    }

    #[test]
    fn tls_connector_construction_verify_ca_keeps_peer_verification_without_hostname()
    -> anyhow::Result<()> {
        let builder = tls_connector_builder(TlsConnectorMode::VerifyCa)?;
        assert_eq!(builder.verify_mode, SslVerifyMode::PEER);
        assert!(builder.disables_hostname_verification);

        let _connector = tls_connector(TlsConnectorMode::VerifyCa)?;
        Ok(())
    }

    #[test]
    fn tls_connector_construction_verify_full_keeps_peer_and_hostname_verification()
    -> anyhow::Result<()> {
        let builder = tls_connector_builder(TlsConnectorMode::VerifyFull)?;
        assert_eq!(builder.verify_mode, SslVerifyMode::PEER);
        assert!(!builder.disables_hostname_verification);

        let _connector = tls_connector(TlsConnectorMode::VerifyFull)?;
        Ok(())
    }
}

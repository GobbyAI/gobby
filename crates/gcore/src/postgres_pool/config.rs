//! Connection settings and TLS connector selection for pooled connections.

use std::time::Duration;

use anyhow::Context;
use deadpool_postgres::{Manager, ManagerConfig};
use tokio_postgres::NoTls;

use crate::postgres::{
    DEFAULT_CONNECT_TIMEOUT, RequestedSslMode, TlsConnectorMode, endpoint_label,
    normalize_sslmode_for_parser, requested_ssl_mode, requested_ssl_mode_from_config,
    tls_connector,
};

/// A parsed pool target: connection settings, requested TLS mode, and a
/// secret-free endpoint label for errors.
pub(super) struct ConnectTarget {
    pub(super) config: tokio_postgres::Config,
    pub(super) mode: RequestedSslMode,
    pub(super) endpoint: String,
}

impl ConnectTarget {
    /// Parse `database_url` with the hub's keepalives (1, 30 s, 10 s, 3) and
    /// the sync path's connect-timeout default.
    pub(super) fn parse(database_url: &str, application_name: &str) -> anyhow::Result<Self> {
        let mut config = normalize_sslmode_for_parser(database_url)
            .parse::<tokio_postgres::Config>()
            .context("failed to parse PostgreSQL connection URL")?;
        config
            .application_name(application_name)
            .keepalives(true)
            .keepalives_idle(Duration::from_secs(30))
            .keepalives_interval(Duration::from_secs(10))
            .keepalives_retries(3);
        if config.get_connect_timeout().is_none() {
            config.connect_timeout(DEFAULT_CONNECT_TIMEOUT);
        }
        let sync_view = postgres::Config::from(config.clone());
        let mode = requested_ssl_mode(database_url)
            .unwrap_or_else(|| requested_ssl_mode_from_config(&sync_view));
        let endpoint = match config.get_dbname() {
            Some(database) => format!("{}/{database}", endpoint_label(&sync_view)),
            None => endpoint_label(&sync_view),
        };
        Ok(Self {
            config,
            mode,
            endpoint,
        })
    }

    /// A connection manager using the connector `postgres.rs::connect_for_mode`
    /// picks for the same mode.
    pub(super) fn manager(&self, manager_config: ManagerConfig) -> anyhow::Result<Manager> {
        let config = self.config.clone();
        let tls_mode = match self.mode {
            RequestedSslMode::Disable => {
                return Ok(Manager::from_config(config, NoTls, manager_config));
            }
            RequestedSslMode::Prefer | RequestedSslMode::Require => TlsConnectorMode::Unverified,
            RequestedSslMode::VerifyCa => TlsConnectorMode::VerifyCa,
            RequestedSslMode::VerifyFull => TlsConnectorMode::VerifyFull,
        };
        Ok(Manager::from_config(
            config,
            tls_connector(tls_mode)?,
            manager_config,
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn config_sets_hub_keepalives() -> anyhow::Result<()> {
        let target = ConnectTarget::parse(
            "postgresql://gobby:secret@db.example:6543/gobby_hub?keepalives=0&application_name=other",
            "gobby-gdaemon-test-1",
        )?;
        let config = &target.config;
        assert!(config.get_keepalives());
        assert_eq!(config.get_keepalives_idle(), Duration::from_secs(30));
        assert_eq!(
            config.get_keepalives_interval(),
            Some(Duration::from_secs(10))
        );
        assert_eq!(config.get_keepalives_retries(), Some(3));
        assert_eq!(config.get_connect_timeout(), Some(&Duration::from_secs(5)));
        assert_eq!(config.get_application_name(), Some("gobby-gdaemon-test-1"));
        assert_eq!(target.endpoint, "db.example:6543/gobby_hub");
        Ok(())
    }

    #[test]
    fn sslmode_selects_the_sync_path_connector_mode() -> anyhow::Result<()> {
        for (sslmode, expected) in [
            ("disable", RequestedSslMode::Disable),
            ("require", RequestedSslMode::Require),
            ("verify-ca", RequestedSslMode::VerifyCa),
            ("'verify-full'", RequestedSslMode::VerifyFull),
        ] {
            let target = ConnectTarget::parse(
                &format!("postgresql://gobby@db.example/gobby_hub?sslmode={sslmode}"),
                "gobby-gdaemon-test-1",
            )?;
            assert_eq!(target.mode, expected, "sslmode={sslmode}");
        }
        let unset =
            ConnectTarget::parse("postgresql://gobby@db.example/gobby_hub", "gobby-gdaemon")?;
        assert_eq!(unset.mode, RequestedSslMode::Prefer);
        Ok(())
    }
}

// A 500 MB ceiling keeps video/audio/PDF inbox imports usable while preventing
// accidental multi-GB reads from exhausting memory before media-specific
// ingestion can stream or degrade the file.
const DEFAULT_MAX_INBOX_ITEM_BYTES: u64 = 500_000_000;

use crate::error::WikiError;
use gobby_core::ai::effective_config::{EffectiveConfigError, daemon_dsn};
use gobby_core::provisioning::{StandaloneConfig, gcore_config_path};
use gobby_core::runtime_mode::{RuntimeMode, runtime_mode};
use std::path::{Path, PathBuf};

const GWIKI_DATABASE_URL_ENV: &str = "GWIKI_DATABASE_URL";
const GOBBY_POSTGRES_DSN_ENV: &str = "GOBBY_POSTGRES_DSN";

pub(crate) fn database_url() -> anyhow::Result<Option<String>> {
    database_url_from_sources(
        database_url_from_env(),
        runtime_mode()?,
        gobby_core::gobby_home,
        daemon_dsn,
    )
}

fn database_url_from_sources(
    env_database_url: Option<String>,
    mode: RuntimeMode,
    home: impl FnOnce() -> anyhow::Result<PathBuf>,
    daemon_dsn: impl FnOnce() -> Result<Option<String>, EffectiveConfigError>,
) -> anyhow::Result<Option<String>> {
    if let Some(database_url) = env_database_url {
        return Ok(Some(database_url));
    }

    let home = home()?;
    if mode == RuntimeMode::Daemon
        && let Some(database_url) = non_empty_trimmed(daemon_dsn()?)
    {
        return Ok(Some(database_url));
    }

    let bootstrap_path = home.join("bootstrap.yaml");
    match gobby_core::bootstrap::postgres_database_url_from_bootstrap_file(&bootstrap_path) {
        Ok(Some(database_url)) => return Ok(Some(database_url)),
        Ok(None) => {}
        Err(error) => {
            log::debug!(
                "failed to resolve gwiki database URL from bootstrap file {}: {error}",
                bootstrap_path.display()
            );
        }
    }
    match mode {
        RuntimeMode::Daemon => Ok(None),
        RuntimeMode::Standalone => resolve_database_url_from_gcore_config(&home),
    }
}

pub(crate) fn database_url_for(command: &str) -> Result<Option<String>, WikiError> {
    database_url().map_err(|error| WikiError::Config {
        detail: format!("failed to resolve PostgreSQL hub for {command}: {error}"),
    })
}

pub(crate) fn database_url_from_env() -> Option<String> {
    [GWIKI_DATABASE_URL_ENV, GOBBY_POSTGRES_DSN_ENV]
        .into_iter()
        .find_map(|name| {
            std::env::var(name)
                .ok()
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
        })
}

fn resolve_database_url_from_gcore_config(home: &Path) -> anyhow::Result<Option<String>> {
    let Some(config) = StandaloneConfig::read_at(&gcore_config_path(home))? else {
        return Ok(None);
    };
    Ok(config
        .get("databases.postgres.dsn")
        .and_then(|value| non_empty_trimmed(Some(value.to_string()))))
}

fn non_empty_trimmed(value: Option<String>) -> Option<String> {
    value
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

pub(crate) fn max_inbox_item_bytes_from_env() -> u64 {
    match std::env::var("GWIKI_MAX_INBOX_ITEM_BYTES") {
        Ok(raw) => parse_positive_u64(&raw).unwrap_or_else(|| {
            eprintln!("warning: ignoring invalid GWIKI_MAX_INBOX_ITEM_BYTES={raw}");
            DEFAULT_MAX_INBOX_ITEM_BYTES
        }),
        Err(_) => DEFAULT_MAX_INBOX_ITEM_BYTES,
    }
}

fn parse_positive_u64(raw: &str) -> Option<u64> {
    raw.trim().parse::<u64>().ok().filter(|value| *value > 0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn positive_u64_env_parser_rejects_invalid_values() {
        assert_eq!(parse_positive_u64("42"), Some(42));
        assert_eq!(parse_positive_u64(" 7 "), Some(7));
        assert_eq!(parse_positive_u64("0"), None);
        assert_eq!(parse_positive_u64("-1"), None);
        assert_eq!(parse_positive_u64("nope"), None);
    }

    #[test]
    fn database_url_logs_bad_bootstrap_and_falls_back_to_gcore_config() {
        let home = tempfile::tempdir().expect("create home");
        fs::write(home.path().join("bootstrap.yaml"), "database_url: [")
            .expect("write bad bootstrap");
        fs::write(
            home.path().join("gcore.yaml"),
            "databases:\n  postgres:\n    dsn: postgresql://gcore.example/gobby\n",
        )
        .expect("write gcore config");
        let resolved = database_url_from_sources(
            None,
            RuntimeMode::Standalone,
            || Ok(home.path().to_path_buf()),
            || panic!("standalone mode must bypass daemon DSN resolution"),
        )
        .expect("resolve database url")
        .expect("gcore database url");

        assert_eq!(resolved, "postgresql://gcore.example/gobby");
    }

    #[test]
    fn database_url_sources_prefer_environment_then_daemon() {
        let home = tempfile::tempdir().expect("create home");
        fs::write(
            home.path().join("bootstrap.yaml"),
            "database_url: postgresql://bootstrap.example/gobby\n",
        )
        .expect("write bootstrap");
        fs::write(
            home.path().join("gcore.yaml"),
            "databases:\n  postgres:\n    dsn: postgresql://gcore.example/gobby\n",
        )
        .expect("write gcore config");

        let env = database_url_from_sources(
            Some("postgresql://env.example/gobby".to_string()),
            RuntimeMode::Daemon,
            || panic!("environment must bypass Gobby home"),
            || panic!("environment must bypass daemon DSN"),
        )
        .expect("environment database url");
        assert_eq!(env.as_deref(), Some("postgresql://env.example/gobby"));

        let daemon = database_url_from_sources(
            None,
            RuntimeMode::Daemon,
            || Ok(home.path().to_path_buf()),
            || Ok(Some(" postgresql://daemon.example/gobby ".to_string())),
        )
        .expect("daemon database url");
        assert_eq!(daemon.as_deref(), Some("postgresql://daemon.example/gobby"));

        let bootstrap = database_url_from_sources(
            None,
            RuntimeMode::Daemon,
            || Ok(home.path().to_path_buf()),
            || Ok(None),
        )
        .expect("bootstrap database url");
        assert_eq!(
            bootstrap.as_deref(),
            Some("postgresql://bootstrap.example/gobby")
        );

        let blank_daemon = database_url_from_sources(
            None,
            RuntimeMode::Daemon,
            || Ok(home.path().to_path_buf()),
            || Ok(Some(" \n\t".to_string())),
        )
        .expect("blank daemon database url");
        assert_eq!(
            blank_daemon.as_deref(),
            Some("postgresql://bootstrap.example/gobby")
        );
    }

    #[test]
    fn daemon_dsn_error_stops_before_bootstrap_and_gcore_yaml() {
        let home = tempfile::tempdir().expect("create home");
        fs::write(
            home.path().join("bootstrap.yaml"),
            "database_url: postgresql://bootstrap.example/gobby\n",
        )
        .expect("write bootstrap");
        fs::write(
            home.path().join("gcore.yaml"),
            "databases:\n  postgres:\n    dsn: postgresql://gcore.example/gobby\n",
        )
        .expect("write gcore config");

        let error = database_url_from_sources(
            None,
            RuntimeMode::Daemon,
            || Ok(home.path().to_path_buf()),
            || {
                Err(
                    gobby_core::ai::effective_config::EffectiveConfigError::Contract {
                        key: "databases.postgres.dsn".to_string(),
                        reason: "test contract failure",
                    },
                )
            },
        )
        .expect_err("daemon DSN error");

        assert!(error.to_string().contains("test contract failure"));
    }

    #[test]
    fn daemon_mode_excludes_gcore_yaml_and_standalone_skips_daemon_dsn() {
        let home = tempfile::tempdir().expect("create home");
        fs::write(
            home.path().join("gcore.yaml"),
            "databases:\n  postgres:\n    dsn: postgresql://gcore.example/gobby\n",
        )
        .expect("write gcore config");

        let daemon = database_url_from_sources(
            None,
            RuntimeMode::Daemon,
            || Ok(home.path().to_path_buf()),
            || Ok(None),
        )
        .expect("daemon database url");
        assert_eq!(daemon, None);

        let standalone = database_url_from_sources(
            None,
            RuntimeMode::Standalone,
            || Ok(home.path().to_path_buf()),
            || panic!("standalone mode must bypass daemon DSN resolution"),
        )
        .expect("standalone database url");
        assert_eq!(
            standalone.as_deref(),
            Some("postgresql://gcore.example/gobby")
        );
    }
}

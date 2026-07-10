// A 500 MB ceiling keeps video/audio/PDF inbox imports usable while preventing
// accidental multi-GB reads from exhausting memory before media-specific
// ingestion can stream or degrade the file.
const DEFAULT_MAX_INBOX_ITEM_BYTES: u64 = 500_000_000;

use crate::error::WikiError;
use gobby_core::provisioning::{StandaloneConfig, gcore_config_path};
use std::path::Path;

const GWIKI_DATABASE_URL_ENV: &str = "GWIKI_DATABASE_URL";
const GOBBY_POSTGRES_DSN_ENV: &str = "GOBBY_POSTGRES_DSN";

pub(crate) fn database_url() -> anyhow::Result<Option<String>> {
    if let Some(database_url) = database_url_from_env() {
        return Ok(Some(database_url));
    }

    let home = gobby_core::gobby_home()?;
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
    resolve_database_url_from_gcore_config(&home)
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

    use crate::support::test_env::EnvGuard;

    #[test]
    fn positive_u64_env_parser_rejects_invalid_values() {
        assert_eq!(parse_positive_u64("42"), Some(42));
        assert_eq!(parse_positive_u64(" 7 "), Some(7));
        assert_eq!(parse_positive_u64("0"), None);
        assert_eq!(parse_positive_u64("-1"), None);
        assert_eq!(parse_positive_u64("nope"), None);
    }

    #[test]
    #[serial_test::serial]
    fn database_url_logs_bad_bootstrap_and_falls_back_to_gcore_config() {
        let home = tempfile::tempdir().expect("create home");
        fs::write(home.path().join("bootstrap.yaml"), "hub_backend: [")
            .expect("write bad bootstrap");
        fs::write(
            home.path().join("gcore.yaml"),
            "databases:\n  postgres:\n    dsn: postgresql://gcore.example/gobby\n",
        )
        .expect("write gcore config");
        let _env = EnvGuard::set("GOBBY_HOME", home.path().as_os_str())
            .and_unset("GWIKI_DATABASE_URL")
            .and_unset("GOBBY_POSTGRES_DSN");

        let resolved = database_url()
            .expect("resolve database url")
            .expect("gcore database url");

        assert_eq!(resolved, "postgresql://gcore.example/gobby");
    }
}

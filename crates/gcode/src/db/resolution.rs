use std::path::{Path, PathBuf};

use anyhow::{Context, bail};
use gobby_core::bootstrap::HubDatabaseBootstrap;
use gobby_core::provisioning::{GCORE_CONFIG_FILENAME, StandaloneConfig};
use gobby_core::runtime_mode::{RuntimeMode, runtime_mode};
use serde::Deserialize;
use uuid::Uuid;

const GCODE_DATABASE_URL_ENV: &str = "GCODE_DATABASE_URL";
const GOBBY_POSTGRES_DSN_ENV: &str = "GOBBY_POSTGRES_DSN";
const MANAGED_EXECUTION_BOOTSTRAP_ENV: &str = "GOBBY_MANAGED_EXECUTION_BOOTSTRAP";

#[derive(Deserialize)]
struct ManagedExecutionBootstrap {
    database_url: String,
    managed_execution_id: String,
}

/// Return Gobby home, respecting `GOBBY_HOME` when the daemon was configured with it.
pub fn gobby_home() -> anyhow::Result<PathBuf> {
    gobby_core::gobby_home()
}

/// Resolve the standalone database from explicit DSN sources.
pub fn resolve_database_url() -> anyhow::Result<String> {
    if let Some(database_url) = resolve_managed_database_url(&mut |name| std::env::var(name).ok())?
    {
        return Ok(database_url);
    }
    let mode = runtime_mode()?;
    let home = gobby_home()?;
    resolve_database_url_from_sources_with_identity_and_reachability(
        &home,
        mode,
        |name| std::env::var(name).ok(),
        daemon_dsn,
        |url| gobby_core::postgres::connect_readonly(url).is_ok(),
        gobby_core::provisioning::probe_postgres_hub_identity,
    )
}

#[cfg(feature = "ai")]
fn daemon_dsn() -> anyhow::Result<Option<String>> {
    gobby_core::ai::effective_config::daemon_dsn().map_err(anyhow::Error::from)
}

#[cfg(not(feature = "ai"))]
fn daemon_dsn() -> anyhow::Result<Option<String>> {
    Ok(None)
}

#[cfg(test)]
fn resolve_database_url_from_sources(
    home: &Path,
    mode: RuntimeMode,
    get_var: impl FnMut(&str) -> Option<String>,
    daemon_dsn: impl FnOnce() -> anyhow::Result<Option<String>>,
    database_reachable: impl FnMut(&str) -> bool,
) -> anyhow::Result<String> {
    resolve_database_url_from_sources_with_identity_and_reachability(
        home,
        mode,
        get_var,
        daemon_dsn,
        database_reachable,
        gobby_core::provisioning::probe_postgres_hub_identity,
    )
}

#[cfg(test)]
fn resolve_database_url_from_sources_with_identity(
    home: &Path,
    mode: RuntimeMode,
    get_var: impl FnMut(&str) -> Option<String>,
    daemon_dsn: impl FnOnce() -> anyhow::Result<Option<String>>,
    database_reachable: impl FnMut(&str) -> bool,
    identity_probe: impl FnMut(&str) -> anyhow::Result<gobby_core::provisioning::HubIdentityProbeResult>,
) -> anyhow::Result<String> {
    resolve_database_url_from_sources_with_identity_and_reachability(
        home,
        mode,
        get_var,
        daemon_dsn,
        database_reachable,
        identity_probe,
    )
}

fn resolve_database_url_from_sources_with_identity_and_reachability(
    home: &Path,
    mode: RuntimeMode,
    mut get_var: impl FnMut(&str) -> Option<String>,
    daemon_dsn: impl FnOnce() -> anyhow::Result<Option<String>>,
    mut database_reachable: impl FnMut(&str) -> bool,
    mut identity_probe: impl FnMut(
        &str,
    )
        -> anyhow::Result<gobby_core::provisioning::HubIdentityProbeResult>,
) -> anyhow::Result<String> {
    let path = home.join("bootstrap.yaml");

    if let Some(database_url) = resolve_managed_database_url(&mut get_var)? {
        return Ok(database_url);
    }

    if let Some(database_url) = resolve_database_url_from_env(&mut get_var) {
        return Ok(database_url);
    }

    if mode == RuntimeMode::Daemon {
        if let Some(database_url) = non_empty_trimmed(daemon_dsn()?) {
            return Ok(database_url);
        }
        if let Some(database_url) = resolve_database_url_from_bootstrap_file(&path)? {
            return Ok(database_url);
        }
        bail!(
            "missing Gobby PostgreSQL configuration. Run `gcode setup --standalone`, set {GCODE_DATABASE_URL_ENV}, or configure the Gobby daemon bootstrap."
        );
    }

    let gcore_database_url = match resolve_database_url_from_gcore_config(home) {
        Ok(database_url) => database_url,
        Err(error) => {
            log::warn!("failed to read gcore config database URL: {error}");
            None
        }
    };

    if let Some(database_url) = resolve_database_url_from_bootstrap_file(&path)? {
        if let Some(database_url) = resolve_recorded_hub_database_url(
            gcore_database_url.as_deref(),
            &database_url,
            &mut database_reachable,
            &mut identity_probe,
        )? {
            return Ok(database_url);
        }
        return Ok(database_url);
    }

    if let Some(database_url) = gcore_database_url {
        return Ok(database_url);
    }

    bail!(
        "missing Gobby PostgreSQL configuration. Run `gcode setup --standalone`, set {GCODE_DATABASE_URL_ENV}, or configure the Gobby daemon bootstrap."
    )
}

fn resolve_managed_database_url(
    get_var: &mut impl FnMut(&str) -> Option<String>,
) -> anyhow::Result<Option<String>> {
    let Some(path) = non_empty_trimmed(get_var(MANAGED_EXECUTION_BOOTSTRAP_ENV)) else {
        return Ok(None);
    };
    let contents = std::fs::read_to_string(&path)
        .with_context(|| format!("failed to read managed execution bootstrap {path}"))?;
    let bootstrap: ManagedExecutionBootstrap = serde_json::from_str(&contents)
        .with_context(|| format!("failed to parse managed execution bootstrap {path}"))?;
    Uuid::parse_str(&bootstrap.managed_execution_id)
        .context("managed execution bootstrap has invalid managed_execution_id")?;
    let database_url = bootstrap.database_url.trim();
    if database_url.is_empty() {
        bail!("managed execution bootstrap has empty database_url");
    }
    Ok(Some(database_url.to_string()))
}

fn resolve_recorded_hub_database_url(
    gcore_database_url: Option<&str>,
    candidate_database_url: &str,
    database_reachable: &mut impl FnMut(&str) -> bool,
    identity_probe: &mut impl FnMut(
        &str,
    )
        -> anyhow::Result<gobby_core::provisioning::HubIdentityProbeResult>,
) -> anyhow::Result<Option<String>> {
    Ok(gobby_core::provisioning::resolve_recorded_hub_database_url(
        gcore_database_url,
        Some(candidate_database_url),
        database_reachable,
        identity_probe,
    )?
    .map(|resolution| resolution.database_url))
}

fn resolve_database_url_from_bootstrap_file(path: &Path) -> anyhow::Result<Option<String>> {
    let Some(bootstrap) = gobby_core::bootstrap::read_hub_database_bootstrap_file(path)? else {
        return Ok(None);
    };
    resolve_database_url_from_bootstrap(&bootstrap).map(Some)
}

fn resolve_database_url_from_gcore_config(home: &Path) -> anyhow::Result<Option<String>> {
    let Some(config) = StandaloneConfig::read_at(&home.join(GCORE_CONFIG_FILENAME))? else {
        return Ok(None);
    };
    Ok(config
        .get("databases.postgres.dsn")
        .and_then(|value| non_empty_trimmed(Some(value.to_string()))))
}

fn resolve_database_url_from_env(
    mut get_var: impl FnMut(&str) -> Option<String>,
) -> Option<String> {
    for name in [GCODE_DATABASE_URL_ENV, GOBBY_POSTGRES_DSN_ENV] {
        if let Some(value) = non_empty_trimmed(get_var(name)) {
            return Some(value);
        }
    }
    None
}

fn resolve_database_url_from_bootstrap(bootstrap: &HubDatabaseBootstrap) -> anyhow::Result<String> {
    bootstrap
        .database_url
        .clone()
        .ok_or_else(|| anyhow::anyhow!("bootstrap.yaml must include `database_url`"))
}

fn non_empty_trimmed(value: Option<String>) -> Option<String> {
    let trimmed = value.as_ref()?.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bootstrap(database_url: Option<&str>) -> HubDatabaseBootstrap {
        HubDatabaseBootstrap {
            database_url: database_url.map(str::to_string),
            daemon_url: None,
        }
    }

    #[test]
    fn database_url_env_prefers_gcode_specific_var() {
        let resolved = resolve_database_url_from_env(|name| match name {
            GCODE_DATABASE_URL_ENV => Some(" postgresql://env/db ".to_string()),
            GOBBY_POSTGRES_DSN_ENV => Some("postgresql://gobby/db".to_string()),
            _ => None,
        });

        assert_eq!(resolved.as_deref(), Some("postgresql://env/db"));
    }

    #[test]
    fn database_url_env_falls_back_to_gobby_postgres_dsn() {
        let resolved = resolve_database_url_from_env(|name| match name {
            GOBBY_POSTGRES_DSN_ENV => Some(" postgresql://gobby/db ".to_string()),
            _ => None,
        });

        assert_eq!(resolved.as_deref(), Some("postgresql://gobby/db"));
    }

    #[test]
    fn database_url_env_ignores_empty_values() {
        let resolved = resolve_database_url_from_env(|name| match name {
            GCODE_DATABASE_URL_ENV => Some("  ".to_string()),
            GOBBY_POSTGRES_DSN_ENV => Some("\n\t".to_string()),
            _ => None,
        });

        assert_eq!(resolved, None);
    }

    #[test]
    fn database_url_sources_prefer_environment() {
        let home = tempfile::tempdir().expect("temp home");

        let resolved = resolve_database_url_from_sources(
            home.path(),
            RuntimeMode::Daemon,
            |name| match name {
                GCODE_DATABASE_URL_ENV => Some("postgresql://env/db".to_string()),
                _ => None,
            },
            || panic!("environment must bypass daemon DSN resolution"),
            |_| true,
        )
        .expect("resolve database url");

        assert_eq!(resolved, "postgresql://env/db");
    }

    #[test]
    fn database_url_sources_prefer_daemon_before_bootstrap_and_gcore() {
        let home = tempfile::tempdir().expect("temp home");
        std::fs::write(
            home.path().join("bootstrap.yaml"),
            "database_url: postgresql://bootstrap/db\n",
        )
        .expect("write bootstrap");
        std::fs::write(
            home.path().join(GCORE_CONFIG_FILENAME),
            "databases.postgres.dsn: postgresql://gcore/db\n",
        )
        .expect("write gcore config");

        let resolved = resolve_database_url_from_sources(
            home.path(),
            RuntimeMode::Daemon,
            |_| None,
            || Ok(Some(" postgresql://daemon/db ".to_string())),
            |_| true,
        )
        .expect("resolve database url");

        assert_eq!(resolved, "postgresql://daemon/db");
    }

    #[test]
    fn blank_daemon_dsn_preserves_bootstrap_precedence() {
        let home = tempfile::tempdir().expect("temp home");
        std::fs::write(
            home.path().join("bootstrap.yaml"),
            "database_url: postgresql://bootstrap/db\n",
        )
        .expect("write bootstrap");

        let resolved = resolve_database_url_from_sources(
            home.path(),
            RuntimeMode::Daemon,
            |_| None,
            || Ok(Some(" \n\t".to_string())),
            |_| true,
        )
        .expect("resolve database url");

        assert_eq!(resolved, "postgresql://bootstrap/db");
    }

    #[test]
    fn daemon_dsn_error_stops_before_bootstrap_and_gcore() {
        let home = tempfile::tempdir().expect("temp home");
        std::fs::write(
            home.path().join("bootstrap.yaml"),
            "database_url: postgresql://bootstrap/db\n",
        )
        .expect("write bootstrap");
        std::fs::write(
            home.path().join(GCORE_CONFIG_FILENAME),
            "databases.postgres.dsn: postgresql://gcore/db\n",
        )
        .expect("write gcore config");

        let error = resolve_database_url_from_sources(
            home.path(),
            RuntimeMode::Daemon,
            |_| None,
            || Err(anyhow::anyhow!("test contract failure")),
            |_| true,
        )
        .expect_err("daemon DSN error must propagate");

        assert!(error.to_string().contains("test contract failure"));
    }

    #[test]
    fn database_url_sources_use_bootstrap_inline_after_environment() {
        let home = tempfile::tempdir().expect("temp home");
        std::fs::write(
            home.path().join("bootstrap.yaml"),
            "database_url: postgresql://inline/db\n",
        )
        .expect("write bootstrap");

        let resolved = resolve_database_url_from_sources(
            home.path(),
            RuntimeMode::Standalone,
            |_| None,
            || panic!("standalone mode must bypass daemon DSN resolution"),
            |_| true,
        )
        .expect("resolve database url");

        assert_eq!(resolved, "postgresql://inline/db");
    }

    #[test]
    fn database_url_sources_use_gcore_after_bootstrap() {
        let home = tempfile::tempdir().expect("temp home");
        std::fs::write(
            home.path().join(GCORE_CONFIG_FILENAME),
            "databases.postgres.dsn: postgresql://gcore/db\n",
        )
        .expect("write gcore config");

        let resolved = resolve_database_url_from_sources(
            home.path(),
            RuntimeMode::Standalone,
            |_| None,
            || panic!("standalone mode must bypass daemon DSN resolution"),
            |_| true,
        )
        .expect("resolve database url");

        assert_eq!(resolved, "postgresql://gcore/db");
    }

    #[test]
    fn adopted_hub_resolves_without_conflict() {
        let home = tempfile::tempdir().expect("temp home");
        std::fs::write(
            home.path().join(GCORE_CONFIG_FILENAME),
            "databases.postgres.dsn: postgresql://adopted/gobby\n",
        )
        .expect("write gcore config");

        let resolved = resolve_database_url_from_sources_with_identity(
            home.path(),
            RuntimeMode::Standalone,
            |_| None,
            || panic!("standalone mode must bypass daemon DSN resolution"),
            |_| true,
            |_| {
                Ok(gobby_core::provisioning::HubIdentityProbeResult::Known(
                    gobby_core::provisioning::HubIdentity {
                        system_identifier: "cluster-a".to_string(),
                        database_name: "gobby".to_string(),
                    },
                ))
            },
        )
        .expect("resolve adopted hub");

        assert_eq!(resolved, "postgresql://adopted/gobby");
    }

    #[test]
    fn daemon_mode_excludes_full_gcore_yaml_from_dsn_resolution() {
        let home = tempfile::tempdir().expect("temp home");
        std::fs::write(
            home.path().join(GCORE_CONFIG_FILENAME),
            "databases.postgres.dsn: postgresql://gcore/db\n",
        )
        .expect("write gcore config");

        let error = resolve_database_url_from_sources(
            home.path(),
            RuntimeMode::Daemon,
            |_| None,
            || Ok(None),
            |_| panic!("daemon mode must not probe a gcore.yaml DSN"),
        )
        .expect_err("daemon mode must not use gcore.yaml");

        assert!(
            error
                .to_string()
                .contains("missing Gobby PostgreSQL configuration")
        );
    }

    #[test]
    fn postgres_bootstrap_accepts_inline_url() {
        let resolved =
            resolve_database_url_from_bootstrap(&bootstrap(Some("postgresql://inline/db")))
                .expect("resolve inline url");

        assert_eq!(resolved, "postgresql://inline/db");
    }

    #[test]
    fn legacy_hub_backend_is_ignored() {
        let bootstrap = gobby_core::bootstrap::parse_hub_database_bootstrap(
            "hub_backend: local-file\ndatabase_url: postgresql://inline/db\n",
        )
        .expect("parse bootstrap")
        .expect("bootstrap data");

        let resolved =
            resolve_database_url_from_bootstrap(&bootstrap).expect("resolve legacy bootstrap");

        assert_eq!(resolved, "postgresql://inline/db");
    }

    #[test]
    fn bootstrap_without_hub_backend_resolves() {
        let bootstrap = gobby_core::bootstrap::parse_hub_database_bootstrap(
            "database_url: postgresql://inline/db\n",
        )
        .expect("parse bootstrap")
        .expect("bootstrap data");

        let resolved =
            resolve_database_url_from_bootstrap(&bootstrap).expect("resolve trimmed bootstrap");

        assert_eq!(resolved, "postgresql://inline/db");
    }

    #[test]
    fn missing_postgres_dsn_fails_clearly() {
        let err = resolve_database_url_from_bootstrap(&bootstrap(None))
            .expect_err("missing dsn must fail");

        assert!(err.to_string().contains("database_url"));
    }

    #[test]
    fn parse_bootstrap_database_reads_database_url() {
        let parsed = gobby_core::bootstrap::parse_hub_database_bootstrap(
            "database_url: postgresql://inline/db\n",
        )
        .expect("parse bootstrap")
        .expect("bootstrap data");

        assert_eq!(
            parsed.database_url.as_deref(),
            Some("postgresql://inline/db")
        );
    }

    #[test]
    fn managed_runtime_prefers_scoped_bootstrap_over_every_shared_source() {
        let home = tempfile::tempdir().expect("temporary home");
        let managed = home.path().join("managed-bootstrap.json");
        std::fs::write(
            &managed,
            r#"{"database_url":"postgresql://scoped/db","managed_execution_id":"a158f699-2c1d-44d8-b438-633d7ad8f8db"}"#,
        )
        .expect("write managed bootstrap");

        let resolved = resolve_database_url_from_sources_with_identity_and_reachability(
            home.path(),
            RuntimeMode::Daemon,
            |name| match name {
                MANAGED_EXECUTION_BOOTSTRAP_ENV => Some(managed.display().to_string()),
                GCODE_DATABASE_URL_ENV => Some("postgresql://operator-env/db".to_string()),
                _ => None,
            },
            || panic!("managed runtime must not query daemon configuration"),
            |_| panic!("managed runtime must not probe shared database reachability"),
            |_| panic!("managed runtime must not probe shared database identity"),
        )
        .expect("resolve managed bootstrap");

        assert_eq!(resolved, "postgresql://scoped/db");
    }

    #[test]
    fn managed_runtime_missing_bootstrap_rejects_shared_fallbacks() {
        let home = tempfile::tempdir().expect("temporary home");
        let missing = home.path().join("missing-managed-bootstrap.json");

        let error = resolve_database_url_from_sources_with_identity_and_reachability(
            home.path(),
            RuntimeMode::Daemon,
            |name| match name {
                MANAGED_EXECUTION_BOOTSTRAP_ENV => Some(missing.display().to_string()),
                GCODE_DATABASE_URL_ENV => Some("postgresql://operator-env/db".to_string()),
                _ => None,
            },
            || panic!("managed runtime must not query daemon configuration"),
            |_| panic!("managed runtime must not probe shared database reachability"),
            |_| panic!("managed runtime must not probe shared database identity"),
        )
        .expect_err("missing managed bootstrap must fail closed");

        assert!(error.to_string().contains("managed execution bootstrap"));
    }
}

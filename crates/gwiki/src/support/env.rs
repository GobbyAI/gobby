// A 500 MB ceiling keeps video/audio/PDF inbox imports usable while preventing
// accidental multi-GB reads from exhausting memory before media-specific
// ingestion can stream or degrade the file.
const DEFAULT_MAX_INBOX_ITEM_BYTES: u64 = 500_000_000;

use crate::error::WikiError;
use gobby_core::config::FalkorConfig;
use gobby_core::grant::{
    AcquireRequest, AcquiredGrant, FalkorCapability, GrantBundle, GrantError, PostgresCapability,
    acquire, acquire_with,
};
use std::cell::RefCell;
use std::path::PathBuf;

thread_local! {
    static ACTIVE_PROJECT_ROOT: RefCell<Option<PathBuf>> = const { RefCell::new(None) };
    static ACTIVE_PROJECT_ID: RefCell<Option<String>> = const { RefCell::new(None) };
}

pub(crate) fn set_active_project_root(root: Option<PathBuf>) {
    ACTIVE_PROJECT_ROOT.with(|slot| *slot.borrow_mut() = root);
}

/// Pin the grant to an explicit project id (`gwiki purge --project-id`): the
/// target's root may no longer exist, so identity must not come from a root.
pub(crate) fn set_active_project_id(project_id: Option<String>) {
    ACTIVE_PROJECT_ID.with(|slot| *slot.borrow_mut() = project_id);
}

pub(crate) fn database_url() -> anyhow::Result<Option<String>> {
    let grant = acquire_runtime_grant()?;
    Ok(Some(postgres_dsn_from_grant(&grant)?))
}

pub(crate) fn database_url_for(command: &str) -> Result<Option<String>, WikiError> {
    database_url().map_err(|error| {
        if let Some(grant) = error.downcast_ref::<GrantError>() {
            return WikiError::from(grant.clone());
        }
        if let Some(grant) = error.downcast_ref::<WikiError>() {
            return match grant {
                WikiError::Grant { source } => WikiError::from(source.clone()),
                other => WikiError::Config {
                    detail: format!("failed to resolve PostgreSQL hub for {command}: {other}"),
                },
            };
        }
        WikiError::Config {
            detail: format!("failed to resolve PostgreSQL hub for {command}: {error}"),
        }
    })
}

fn acquire_runtime_grant() -> Result<AcquiredGrant, GrantError> {
    if let Some(project_id) = ACTIVE_PROJECT_ID.with(|slot| slot.borrow().clone()) {
        return acquire_with(&AcquireRequest::from_process_for_project_id(&project_id));
    }
    let root = project_root_for_grant().ok_or(GrantError::DaemonRequired)?;
    acquire(root)
}

pub(crate) fn project_root_for_grant() -> Option<PathBuf> {
    ACTIVE_PROJECT_ROOT
        .with(|slot| slot.borrow().clone())
        .or_else(|| {
            let cwd = std::env::current_dir().ok()?;
            gobby_core::project::find_project_root(&cwd)
        })
}

fn postgres_dsn_from_grant(grant: &AcquiredGrant) -> Result<String, GrantError> {
    match &grant.bundle.capabilities.postgres {
        PostgresCapability::Direct { dsn, .. } if !dsn.trim().is_empty() => Ok(dsn.clone()),
        _ => Err(GrantError::Malformed(
            "postgres capability is not a direct DSN".into(),
        )),
    }
}

/// Resolve FalkorDB from the acquired v2 grant capability, not hub settings.
///
/// `ai_source_for_conn` rejects secret-reference keys, so
/// `databases.falkordb.password` never appears on that source even when the
/// grant's Direct capability carries AUTH material.
pub(crate) fn falkordb_config() -> Result<Option<FalkorConfig>, WikiError> {
    let grant = acquire_runtime_grant().map_err(WikiError::from)?;
    Ok(falkor_from_grant(&grant.bundle))
}

pub(crate) fn falkor_from_grant(grant: &GrantBundle) -> Option<FalkorConfig> {
    match &grant.capabilities.falkordb {
        FalkorCapability::Direct {
            host,
            port,
            password,
        } => Some(FalkorConfig {
            host: host.clone(),
            port: u16::try_from(*port).unwrap_or(6379),
            password: (!password.is_empty()).then(|| password.clone()),
        }),
        FalkorCapability::Brokered { .. } | FalkorCapability::Unavailable {} => None,
    }
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

    #[test]
    fn positive_u64_env_parser_rejects_invalid_values() {
        assert_eq!(parse_positive_u64("42"), Some(42));
        assert_eq!(parse_positive_u64(" 7 "), Some(7));
        assert_eq!(parse_positive_u64("0"), None);
        assert_eq!(parse_positive_u64("-1"), None);
        assert_eq!(parse_positive_u64("nope"), None);
    }

    #[test]
    fn env_and_bootstrap_dsn_helpers_are_gone() {
        let source = include_str!("env.rs");
        let production = source
            .split("#[cfg(test)]")
            .next()
            .expect("production source");
        for forbidden in [
            "GWIKI_TEST_DATABASE_URL",
            "GOBBY_TEST_POSTGRES_DSN",
            "database_url_from_sources",
            "postgres_database_url_from_bootstrap_file",
            "daemon_dsn",
        ] {
            assert!(
                !production.contains(forbidden),
                "gwiki env.rs still contains {forbidden}"
            );
        }
        assert!(source.contains("acquire("));
    }

    #[test]
    fn falkor_from_grant_keeps_direct_capability_password() {
        let grant = gobby_core::grant::managed_direct_grant(
            "proj",
            "machine",
            &gobby_core::grant::DirectConnections::postgres("postgres://x").with_falkor(
                "127.0.0.1",
                16379,
                Some("grant-pass"),
            ),
        );
        let config = falkor_from_grant(&grant).expect("direct capability");
        assert_eq!(config.host, "127.0.0.1");
        assert_eq!(config.port, 16379);
        assert_eq!(config.password.as_deref(), Some("grant-pass"));
    }

    #[test]
    fn falkor_from_grant_empty_direct_password_is_absent() {
        let grant = gobby_core::grant::managed_direct_grant(
            "proj",
            "machine",
            &gobby_core::grant::DirectConnections::postgres("postgres://x").with_falkor(
                "127.0.0.1",
                16379,
                None,
            ),
        );
        let config = falkor_from_grant(&grant).expect("direct capability");
        assert_eq!(config.password, None);
    }

    #[test]
    fn falkor_from_grant_unavailable_is_none() {
        let grant = gobby_core::grant::managed_direct_grant(
            "proj",
            "machine",
            &gobby_core::grant::DirectConnections::postgres("postgres://x"),
        );
        assert!(falkor_from_grant(&grant).is_none());
    }
}

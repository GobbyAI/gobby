// A 500 MB ceiling keeps video/audio/PDF inbox imports usable while preventing
// accidental multi-GB reads from exhausting memory before media-specific
// ingestion can stream or degrade the file.
const DEFAULT_MAX_INBOX_ITEM_BYTES: u64 = 500_000_000;

use crate::error::WikiError;
use gobby_core::grant::{AcquiredGrant, GrantError, PostgresCapability, acquire};
use std::cell::RefCell;
use std::path::PathBuf;

thread_local! {
    static ACTIVE_PROJECT_ROOT: RefCell<Option<PathBuf>> = const { RefCell::new(None) };
}

pub(crate) fn set_active_project_root(root: Option<PathBuf>) {
    ACTIVE_PROJECT_ROOT.with(|slot| *slot.borrow_mut() = root);
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
    let root = project_root_for_grant().ok_or(GrantError::DaemonRequired)?;
    acquire(root)
}

fn project_root_for_grant() -> Option<PathBuf> {
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
}

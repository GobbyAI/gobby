use std::path::PathBuf;

use gobby_core::grant::{
    AcquiredGrant, FalkorCapability, GrantBundle, PostgresCapability, QdrantCapability,
};

use crate::cli_error::CliError;
use crate::config::{FALKORDB_GRAPH_NAME, FalkorConfig, QdrantConfig};

/// Return Gobby home, respecting `GOBBY_HOME` when the daemon was configured with it.
#[allow(dead_code)]
pub fn gobby_home() -> anyhow::Result<PathBuf> {
    gobby_core::gobby_home()
}

/// Extract the scoped-role PostgreSQL DSN from an acquired grant.
pub fn postgres_dsn_from_grant(grant: &GrantBundle) -> Result<String, CliError> {
    match &grant.capabilities.postgres {
        PostgresCapability::Direct { dsn, .. } if !dsn.trim().is_empty() => Ok(dsn.clone()),
        PostgresCapability::Direct { .. } => Err(CliError::grant(
            gobby_core::grant::GrantError::Malformed("empty postgres DSN".into()),
        )),
        PostgresCapability::Brokered { .. } => Err(CliError::grant(
            gobby_core::grant::GrantError::DaemonRequired,
        )),
        PostgresCapability::Unavailable {} => Err(CliError::capability_unavailable("postgres")),
    }
}

pub fn falkor_from_grant(grant: &GrantBundle) -> Option<FalkorConfig> {
    match &grant.capabilities.falkordb {
        FalkorCapability::Direct {
            host,
            port,
            password,
        } => Some(FalkorConfig {
            host: host.clone(),
            port: u16::try_from(*port).ok()?,
            password: (!password.is_empty()).then(|| password.clone()),
            graph_name: FALKORDB_GRAPH_NAME.to_string(),
        }),
        FalkorCapability::Brokered { .. } | FalkorCapability::Unavailable {} => None,
    }
}

pub fn qdrant_from_grant(grant: &GrantBundle) -> Option<QdrantConfig> {
    match &grant.capabilities.qdrant {
        QdrantCapability::Direct { url, api_key } => Some(QdrantConfig {
            url: (!url.is_empty()).then(|| url.clone()),
            api_key: (!api_key.is_empty()).then(|| api_key.clone()),
        }),
        QdrantCapability::Brokered { .. } | QdrantCapability::Unavailable {} => None,
    }
}

pub fn database_url_from_acquired(grant: &AcquiredGrant) -> Result<String, CliError> {
    postgres_dsn_from_grant(&grant.bundle)
}

/// Acquire a project grant from the current project root and return its DSN.
pub fn resolve_database_url() -> anyhow::Result<String> {
    let root = crate::config::detect_project_root()
        .map_err(|error| error.context(CliError::project_required()))?;
    let grant = gobby_core::grant::acquire(root).map_err(CliError::grant)?;
    Ok(database_url_from_acquired(&grant)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_core::grant::{
        AiCapability, GrantCapabilities, GrantDeployment, GrantPrincipal, PrincipalKind,
        expected_schema_identity,
    };

    fn grant_with_postgres(dsn: &str) -> GrantBundle {
        GrantBundle {
            version: 2,
            api_contract: 1,
            config_revision: 1,
            deployment: GrantDeployment {
                token: "tok".into(),
                fencing_epoch: 1,
            },
            schema_identity: expected_schema_identity(),
            principal: GrantPrincipal {
                kind: PrincipalKind::Interactive,
                machine_id: "machine".into(),
                project_id: "project".into(),
                execution_id: None,
                session_id: None,
            },
            capabilities: GrantCapabilities {
                postgres: PostgresCapability::Direct {
                    dsn: dsn.into(),
                    role_name: "role".into(),
                    credential_generation: 1,
                    valid_until: 9_999_999_999,
                },
                falkordb: FalkorCapability::Unavailable {},
                qdrant: QdrantCapability::Unavailable {},
                embed: AiCapability::Unavailable {},
                text_generate: AiCapability::Unavailable {},
                tool_chat: AiCapability::Unavailable {},
                vision_extract: AiCapability::Unavailable {},
                audio_transcribe: AiCapability::Unavailable {},
                broker_operations: Vec::new(),
            },
            issued_at: 1,
            expires_at: 9_999_999_999,
            payload_checksum: String::new(),
            signature: String::new(),
        }
    }

    #[test]
    fn grant_dsn_is_the_only_resolution_source() {
        let grant = grant_with_postgres("postgresql://grant.example/gobby");
        assert_eq!(
            postgres_dsn_from_grant(&grant).expect("dsn"),
            "postgresql://grant.example/gobby"
        );
        let source = include_str!("resolution.rs");
        let production = source
            .split("#[cfg(test)]")
            .next()
            .expect("production source");
        assert!(
            production.contains("postgres_dsn_from_grant"),
            "production slice must still contain postgres_dsn_from_grant"
        );
        for forbidden in [
            "GCODE_TEST_DATABASE_URL",
            "GOBBY_TEST_POSTGRES_DSN",
            "GWIKI_TEST_DATABASE_URL",
            "daemon_dsn",
            "resolve_database_url_from_env",
            "resolve_database_url_from_bootstrap",
            "resolve_database_url_from_gcore_config",
        ] {
            assert!(
                !production.contains(forbidden),
                "resolution.rs still contains {forbidden}"
            );
        }
    }

    fn grant_with_postgres_capability(postgres: PostgresCapability) -> GrantBundle {
        let mut grant = grant_with_postgres("postgresql://grant.example/gobby");
        grant.capabilities.postgres = postgres;
        grant
    }

    #[test]
    fn postgres_dsn_from_grant_distinguishes_capability_variants() {
        let direct = grant_with_postgres("postgresql://grant.example/gobby");
        assert_eq!(
            postgres_dsn_from_grant(&direct).expect("dsn"),
            "postgresql://grant.example/gobby"
        );

        let empty = grant_with_postgres("   ");
        let empty_err = postgres_dsn_from_grant(&empty).expect_err("empty dsn");
        assert_eq!(empty_err.code, "malformed");

        let brokered = grant_with_postgres_capability(PostgresCapability::Brokered {
            operations: Vec::new(),
        });
        let brokered_err = postgres_dsn_from_grant(&brokered).expect_err("brokered");
        assert_eq!(brokered_err.code, "daemon_required");

        let unavailable = grant_with_postgres_capability(PostgresCapability::Unavailable {});
        let unavailable_err = postgres_dsn_from_grant(&unavailable).expect_err("unavailable");
        assert_eq!(unavailable_err.code, "capability_unavailable");
    }

    #[test]
    fn falkor_from_grant_rejects_out_of_range_port() {
        let mut grant = grant_with_postgres("postgresql://grant.example/gobby");
        grant.capabilities.falkordb = FalkorCapability::Direct {
            host: "127.0.0.1".into(),
            port: 70_000,
            password: String::new(),
        };
        assert!(falkor_from_grant(&grant).is_none());

        grant.capabilities.falkordb = FalkorCapability::Direct {
            host: "127.0.0.1".into(),
            port: 6380,
            password: String::new(),
        };
        let config = falkor_from_grant(&grant).expect("valid port");
        assert_eq!(config.port, 6380);
    }
}

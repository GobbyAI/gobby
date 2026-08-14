use std::path::PathBuf;

use gobby_core::grant::{
    AcquiredGrant, FalkorCapability, GrantBundle, PostgresCapability, QdrantCapability,
};

use crate::cli_error::CliError;
use crate::config::{FALKORDB_GRAPH_NAME, FalkorConfig, QdrantConfig};

/// Return Gobby home, respecting `GOBBY_HOME` when the daemon was configured with it.
pub fn gobby_home() -> anyhow::Result<PathBuf> {
    gobby_core::gobby_home()
}

/// Extract the scoped-role PostgreSQL DSN from an acquired grant.
pub fn postgres_dsn_from_grant(grant: &GrantBundle) -> Result<String, CliError> {
    match &grant.capabilities.postgres {
        PostgresCapability::Direct { dsn, .. } if !dsn.trim().is_empty() => Ok(dsn.clone()),
        _ => Err(CliError::grant(gobby_core::grant::GrantError::Malformed(
            "postgres capability is not a direct DSN".into(),
        ))),
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
            port: u16::try_from(*port).unwrap_or(6379),
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
    let root = crate::config::detect_project_root().map_err(|_| CliError::project_required())?;
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
        for forbidden in [
            "GCODE_DATABASE_URL",
            "GOBBY_POSTGRES_DSN",
            "GWIKI_DATABASE_URL",
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
}

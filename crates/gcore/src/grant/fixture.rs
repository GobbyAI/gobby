//! Test helpers that mint managed grant files instead of injecting DSNs.

use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use super::{
    AiCapability, EXPECTED_API_CONTRACT, FalkorCapability, GRANT_VERSION, GrantBundle,
    GrantCapabilities, GrantDeployment, GrantPrincipal, PostgresCapability, PrincipalKind,
    QdrantCapability, expected_schema_identity, write_grant_file,
};

pub const TEST_DEPLOYMENT_TOKEN: &str = "cafebabedeadbeef";

#[derive(Clone, Debug)]
pub struct DirectConnections {
    pub postgres_dsn: String,
    pub falkor_host: Option<String>,
    pub falkor_port: i64,
    pub falkor_password: String,
    pub qdrant_url: Option<String>,
    pub qdrant_api_key: String,
}

impl DirectConnections {
    pub fn postgres(dsn: impl Into<String>) -> Self {
        Self {
            postgres_dsn: dsn.into(),
            falkor_host: None,
            falkor_port: 6379,
            falkor_password: String::new(),
            qdrant_url: None,
            qdrant_api_key: String::new(),
        }
    }

    pub fn with_falkor(
        mut self,
        host: impl Into<String>,
        port: i64,
        password: Option<&str>,
    ) -> Self {
        self.falkor_host = Some(host.into());
        self.falkor_port = port;
        self.falkor_password = password.unwrap_or_default().to_string();
        self
    }

    pub fn with_qdrant(mut self, url: impl Into<String>, api_key: Option<&str>) -> Self {
        self.qdrant_url = Some(url.into());
        self.qdrant_api_key = api_key.unwrap_or_default().to_string();
        self
    }
}

pub fn managed_direct_grant(
    project_id: &str,
    machine_id: &str,
    connections: &DirectConnections,
) -> GrantBundle {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(1_700_000_000);
    let falkordb = match &connections.falkor_host {
        Some(host) => FalkorCapability::Direct {
            host: host.clone(),
            port: connections.falkor_port,
            password: connections.falkor_password.clone(),
        },
        None => FalkorCapability::Unavailable {},
    };
    let qdrant = match &connections.qdrant_url {
        Some(url) => QdrantCapability::Direct {
            url: url.clone(),
            api_key: connections.qdrant_api_key.clone(),
        },
        None => QdrantCapability::Unavailable {},
    };
    GrantBundle {
        version: GRANT_VERSION,
        api_contract: EXPECTED_API_CONTRACT,
        config_revision: 1,
        deployment: GrantDeployment {
            token: TEST_DEPLOYMENT_TOKEN.to_string(),
            fencing_epoch: 1,
        },
        schema_identity: expected_schema_identity(),
        principal: GrantPrincipal {
            kind: PrincipalKind::AgentRun,
            machine_id: machine_id.to_string(),
            project_id: project_id.to_string(),
            execution_id: Some("grant-fixture".to_string()),
            session_id: Some("grant-fixture".to_string()),
            code_overlay_project_id: None,
        },
        capabilities: GrantCapabilities {
            postgres: PostgresCapability::Direct {
                dsn: connections.postgres_dsn.clone(),
                role_name: "gobby_grant_fixture".to_string(),
                credential_generation: 1,
                valid_until: now + 3_600,
            },
            falkordb,
            qdrant,
            embed: AiCapability::Unavailable {},
            text_generate: AiCapability::Unavailable {},
            tool_chat: AiCapability::Unavailable {},
            vision_extract: AiCapability::Unavailable {},
            audio_transcribe: AiCapability::Unavailable {},
            broker_operations: Vec::new(),
        },
        issued_at: now - 10,
        expires_at: now + 3_600,
        payload_checksum: String::new(),
        signature: String::new(),
    }
    .with_checksum()
}

pub fn write_managed_bootstrap(
    dest_dir: &Path,
    grant: &GrantBundle,
) -> Result<PathBuf, super::GrantError> {
    std::fs::create_dir_all(dest_dir).map_err(|error| super::GrantError::Io(error.to_string()))?;
    let path = dest_dir.join("grant.json");
    write_grant_file(&path, grant)?;
    Ok(path)
}

//! v2 grant bundle types, canonical JSON, and payload checksums.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use super::{GrantError, hex_encode, sha256};

pub const GRANT_VERSION: i64 = 2;
pub const EXPECTED_API_CONTRACT: i64 = 1;

#[cfg(not(feature = "postgres"))]
const GOLDEN_BASELINE_CHECKSUM: &str =
    "86ad53076df7527fa0b832dd62b91037954a62d301957ef09b0c9f0f69331b33";
#[cfg(not(feature = "postgres"))]
const GOLDEN_ASSETS_ROOT_HASH: &str =
    "466dc3eac74063bfdf1f92edc72c6c33fa918bca7f23bde665e53c887ab12093";

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BrokerOperation {
    pub name: String,
    pub method: String,
    pub path: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case", deny_unknown_fields)]
pub enum PostgresCapability {
    Direct {
        dsn: String,
        role_name: String,
        credential_generation: i64,
        valid_until: i64,
    },
    Brokered {
        operations: Vec<BrokerOperation>,
    },
    Unavailable {},
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case", deny_unknown_fields)]
pub enum FalkorCapability {
    Direct {
        host: String,
        port: i64,
        password: String,
    },
    Brokered {
        operations: Vec<BrokerOperation>,
    },
    Unavailable {},
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case", deny_unknown_fields)]
pub enum QdrantCapability {
    Direct { url: String, api_key: String },
    Brokered { operations: Vec<BrokerOperation> },
    Unavailable {},
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case", deny_unknown_fields)]
pub enum AiCapability {
    Daemon {},
    Unavailable {},
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GrantDeployment {
    pub token: String,
    pub fencing_epoch: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GrantSchemaIdentity {
    pub runner_protocol: i64,
    pub baseline_version: i64,
    pub baseline_checksum: String,
    pub latest_version: i64,
    pub latest_checksum: String,
    pub assets_root_hash: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PrincipalKind {
    Interactive,
    AgentRun,
    ToolChat,
}

impl PrincipalKind {
    pub fn is_managed(self) -> bool {
        matches!(self, Self::AgentRun | Self::ToolChat)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GrantPrincipal {
    pub kind: PrincipalKind,
    pub machine_id: String,
    pub project_id: String,
    #[serde(default)]
    pub execution_id: Option<String>,
    #[serde(default)]
    pub session_id: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GrantCapabilities {
    pub postgres: PostgresCapability,
    pub falkordb: FalkorCapability,
    pub qdrant: QdrantCapability,
    pub embed: AiCapability,
    pub text_generate: AiCapability,
    pub tool_chat: AiCapability,
    pub vision_extract: AiCapability,
    pub audio_transcribe: AiCapability,
    pub broker_operations: Vec<BrokerOperation>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GrantBundle {
    pub version: i64,
    pub api_contract: i64,
    pub config_revision: i64,
    pub deployment: GrantDeployment,
    pub schema_identity: GrantSchemaIdentity,
    pub principal: GrantPrincipal,
    pub capabilities: GrantCapabilities,
    pub issued_at: i64,
    pub expires_at: i64,
    #[serde(default)]
    pub payload_checksum: String,
    #[serde(default)]
    pub signature: String,
}

impl GrantBundle {
    pub fn with_checksum(mut self) -> Self {
        self.payload_checksum = payload_checksum(&self);
        self
    }

    pub fn credential_generation(&self) -> Option<i64> {
        match &self.capabilities.postgres {
            PostgresCapability::Direct {
                credential_generation,
                ..
            } => Some(*credential_generation),
            _ => None,
        }
    }

    pub fn remaining_ttl(&self, now: i64) -> i64 {
        self.expires_at.saturating_sub(now)
    }

    pub fn total_ttl(&self) -> i64 {
        self.expires_at.saturating_sub(self.issued_at).max(1)
    }

    pub fn is_expired(&self, now: i64) -> bool {
        now >= self.expires_at
    }

    pub fn past_half_ttl(&self, now: i64) -> bool {
        self.remaining_ttl(now) * 2 < self.total_ttl()
    }

    pub fn model_dump_canonical(&self) -> Result<Vec<u8>, GrantError> {
        let value =
            serde_json::to_value(self).map_err(|error| GrantError::Malformed(error.to_string()))?;
        let mut encoded = serde_json::to_string(&sort_value(value))
            .map_err(|error| GrantError::Malformed(error.to_string()))?;
        encoded.push('\n');
        Ok(encoded.into_bytes())
    }
}

pub fn expected_schema_identity() -> GrantSchemaIdentity {
    #[cfg(feature = "postgres")]
    {
        let identity = crate::schema::schema_identity();
        GrantSchemaIdentity {
            runner_protocol: i64::from(identity.runner_protocol_version),
            baseline_version: i64::from(identity.baseline.version),
            baseline_checksum: identity.baseline.checksum.to_string(),
            latest_version: i64::from(identity.latest_asset.version),
            latest_checksum: identity.latest_asset.checksum.to_string(),
            assets_root_hash: identity.root_hash,
        }
    }
    #[cfg(not(feature = "postgres"))]
    {
        GrantSchemaIdentity {
            runner_protocol: 1,
            baseline_version: 375,
            baseline_checksum: GOLDEN_BASELINE_CHECKSUM.to_string(),
            latest_version: 375,
            latest_checksum: GOLDEN_BASELINE_CHECKSUM.to_string(),
            assets_root_hash: GOLDEN_ASSETS_ROOT_HASH.to_string(),
        }
    }
}

pub fn canonical_payload_bytes(grant: &GrantBundle) -> Result<Vec<u8>, GrantError> {
    let mut value =
        serde_json::to_value(grant).map_err(|error| GrantError::Malformed(error.to_string()))?;
    if let Value::Object(map) = &mut value {
        map.remove("payload_checksum");
        map.remove("signature");
    }
    serde_json::to_vec(&sort_value(value)).map_err(|error| GrantError::Malformed(error.to_string()))
}

pub fn payload_checksum(grant: &GrantBundle) -> String {
    match canonical_payload_bytes(grant) {
        Ok(bytes) => hex_encode(&sha256(&bytes)),
        Err(_) => String::new(),
    }
}

pub fn verify_payload_checksum(grant: &GrantBundle) -> Result<(), GrantError> {
    let expected = payload_checksum(grant);
    if expected.is_empty() || expected != grant.payload_checksum {
        return Err(GrantError::Malformed(
            "payload checksum mismatch".to_string(),
        ));
    }
    Ok(())
}

pub fn parse_grant_json(raw: &[u8]) -> Result<GrantBundle, GrantError> {
    let grant: GrantBundle = serde_json::from_slice(raw.trim_ascii())
        .map_err(|error| GrantError::Malformed(error.to_string()))?;
    verify_payload_checksum(&grant)?;
    Ok(grant)
}

pub fn validate_for_construction(
    grant: &GrantBundle,
    expected_project: &str,
    expected_machine: &str,
    expected_deployment: Option<&str>,
    require_managed: bool,
) -> Result<(), GrantError> {
    if grant.version != GRANT_VERSION {
        return Err(GrantError::Malformed(format!(
            "unsupported grant version {}",
            grant.version
        )));
    }
    verify_payload_checksum(grant)?;
    if grant.api_contract != EXPECTED_API_CONTRACT {
        return Err(GrantError::ApiContractMismatch);
    }
    if grant.schema_identity != expected_schema_identity() {
        return Err(GrantError::SchemaMismatch);
    }
    if grant.principal.project_id != expected_project {
        return Err(GrantError::Malformed(
            "grant project does not match local project".to_string(),
        ));
    }
    if grant.principal.machine_id != expected_machine {
        return Err(GrantError::Malformed(
            "grant machine does not match local machine".to_string(),
        ));
    }
    if let Some(expected) = expected_deployment
        && grant.deployment.token != expected
    {
        return Err(GrantError::DeploymentMismatch);
    }
    if require_managed && !grant.principal.kind.is_managed() {
        return Err(GrantError::Malformed(
            "managed grant must carry an agent_run or tool_chat principal".to_string(),
        ));
    }
    if !require_managed && grant.principal.kind != PrincipalKind::Interactive {
        return Err(GrantError::Malformed(
            "interactive cache requires an interactive principal".to_string(),
        ));
    }
    Ok(())
}

fn sort_value(value: Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut sorted = BTreeMap::new();
            for (key, child) in map {
                sorted.insert(key, sort_value(child));
            }
            let mut object = Map::new();
            for (key, child) in sorted {
                object.insert(key, child);
            }
            Value::Object(object)
        }
        Value::Array(items) => Value::Array(items.into_iter().map(sort_value).collect()),
        other => other,
    }
}

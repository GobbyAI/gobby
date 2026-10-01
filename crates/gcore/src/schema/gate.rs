use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::{Component, Path};

use serde::{Deserialize, Serialize};
use time::OffsetDateTime;
use time::format_description::well_known::Rfc3339;

const MANIFEST_FORMAT: &str = "gobby-hub-backup-manifest";
const MANIFEST_VERSION: u32 = 3;
const STORE_KEYS: [&str; 5] = ["falkordb", "files", "postgres", "qdrant", "volumes"];

#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct VerificationState {
    pub verified: bool,
    pub method: Option<String>,
    pub timestamp: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ArtifactRecord {
    pub name: String,
    pub path: String,
    pub sha256: String,
    pub size_bytes: u64,
}

#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SourceIdentity {
    pub pg_system_identifier: String,
    pub database_name: String,
    pub database_oid: u32,
}

#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct StoreRecord {
    pub archive_verified: VerificationState,
    pub restore_verified: VerificationState,
    pub details: serde_json::Value,
}

#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct HubBackupManifest {
    pub manifest_format: String,
    pub manifest_version: u32,
    pub created_at: String,
    pub gobby_version: String,
    pub epoch_id: Option<String>,
    pub source_identity: SourceIdentity,
    pub backup_starting_head: i32,
    pub row_count_probes: BTreeMap<String, u64>,
    pub artifacts: Vec<ArtifactRecord>,
    pub stores: BTreeMap<String, StoreRecord>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BackupManifestError {
    reasons: Vec<String>,
}

impl BackupManifestError {
    fn new(reason: impl Into<String>) -> Self {
        Self {
            reasons: vec![reason.into()],
        }
    }

    fn from_reasons(reasons: Vec<String>) -> Self {
        Self { reasons }
    }

    pub fn reasons(&self) -> &[String] {
        &self.reasons
    }
}

impl fmt::Display for BackupManifestError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "backup manifest rejected: {}",
            self.reasons.join("; ")
        )
    }
}

impl std::error::Error for BackupManifestError {}

pub fn parse_backup_manifest(payload: &str) -> Result<HubBackupManifest, BackupManifestError> {
    let manifest: HubBackupManifest = serde_json::from_str(payload)
        .map_err(|error| BackupManifestError::new(format!("invalid JSON shape: {error}")))?;
    let mut reasons = Vec::new();
    if manifest.manifest_format != MANIFEST_FORMAT {
        reasons.push(format!(
            "manifest_format must be {MANIFEST_FORMAT:?}, got {:?}",
            manifest.manifest_format
        ));
    }
    if manifest.manifest_version != MANIFEST_VERSION {
        reasons.push(format!(
            "manifest_version must be {MANIFEST_VERSION}, got {}",
            manifest.manifest_version
        ));
    }
    if OffsetDateTime::parse(&manifest.created_at, &Rfc3339).is_err() {
        reasons.push("created_at must be RFC3339".to_owned());
    }
    let observed_keys: BTreeSet<&str> = manifest.stores.keys().map(String::as_str).collect();
    let expected_keys: BTreeSet<&str> = STORE_KEYS.into_iter().collect();
    if observed_keys != expected_keys {
        reasons.push(format!(
            "stores must contain exactly {:?}, got {:?}",
            expected_keys, observed_keys
        ));
    }
    for (key, store) in &manifest.stores {
        if !store.details.is_object() {
            reasons.push(format!("store details must be an object: {key}"));
        }
    }
    let mut artifact_names = BTreeSet::new();
    let mut artifact_paths = BTreeSet::new();
    for artifact in &manifest.artifacts {
        if artifact.name.is_empty() || !artifact_names.insert(&artifact.name) {
            reasons.push(format!(
                "artifact name is empty or duplicated: {:?}",
                artifact.name
            ));
        }
        if !valid_relative_path(&artifact.path) || !artifact_paths.insert(&artifact.path) {
            reasons.push(format!(
                "artifact path is unsafe or duplicated: {:?}",
                artifact.path
            ));
        }
        if artifact.sha256.len() != 64
            || !artifact
                .sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        {
            reasons.push(format!("artifact sha256 is invalid: {}", artifact.name));
        }
    }
    if reasons.is_empty() {
        Ok(manifest)
    } else {
        Err(BackupManifestError::from_reasons(reasons))
    }
}

fn valid_relative_path(value: &str) -> bool {
    let path = Path::new(value);
    !value.is_empty()
        && !path.is_absolute()
        && path
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parser_rejects_wrong_version_and_unsafe_artifact_paths() {
        let fixture = include_str!("../../tests/fixtures/hub_backup_manifest/v3_roundtrip.json");
        let mut value: serde_json::Value = serde_json::from_str(fixture).expect("fixture JSON");
        value["manifest_version"] = 4.into();
        value["artifacts"][0]["path"] = "../escape".into();
        value["stores"]["postgres"]["details"] = serde_json::Value::Array(Vec::new());

        let error = parse_backup_manifest(&value.to_string()).expect_err("must reject drift");
        assert!(
            error
                .reasons()
                .iter()
                .any(|reason| reason.contains("manifest_version"))
        );
        assert!(
            error
                .reasons()
                .iter()
                .any(|reason| reason.contains("unsafe"))
        );
        assert!(
            error
                .reasons()
                .iter()
                .any(|reason| reason.contains("details"))
        );
    }
}

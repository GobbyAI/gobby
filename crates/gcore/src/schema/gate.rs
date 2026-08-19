use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fs::{self, File};
use std::io::{BufReader, Read};
use std::path::{Component, Path};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use time::format_description::well_known::Rfc3339;
use time::{Duration, OffsetDateTime};

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

#[derive(Clone, Copy, Debug)]
pub struct BackupGateContext<'a> {
    pub backup_root: &'a Path,
    pub current_identity: &'a SourceIdentity,
    pub current_schema_head: i32,
    pub now: OffsetDateTime,
    pub max_age: Duration,
}

impl<'a> BackupGateContext<'a> {
    pub fn new(
        backup_root: &'a Path,
        current_identity: &'a SourceIdentity,
        current_schema_head: i32,
        now: OffsetDateTime,
    ) -> Self {
        Self {
            backup_root,
            current_identity,
            current_schema_head,
            now,
            max_age: Duration::hours(24),
        }
    }
}

#[derive(Clone, Debug)]
pub struct VerifiedBackupManifest {
    manifest: HubBackupManifest,
}

impl VerifiedBackupManifest {
    pub fn verify(
        manifest: HubBackupManifest,
        context: &BackupGateContext<'_>,
    ) -> Result<Self, BackupManifestError> {
        let mut reasons = Vec::new();
        let created_at = OffsetDateTime::parse(&manifest.created_at, &Rfc3339)
            .map_err(|error| BackupManifestError::new(format!("invalid created_at: {error}")))?;
        let age = context.now - created_at;
        if age.is_negative() {
            reasons.push(format!(
                "manifest creation time is in the future: {}",
                manifest.created_at
            ));
        } else if age > context.max_age {
            reasons.push(format!(
                "manifest exceeds max age: {} seconds > {} seconds",
                age.whole_seconds(),
                context.max_age.whole_seconds()
            ));
        }
        if &manifest.source_identity != context.current_identity {
            reasons.push("source identity fingerprint mismatch".to_owned());
        }
        if manifest.backup_starting_head != context.current_schema_head {
            reasons.push(format!(
                "backup starting head {} does not match current head {}",
                manifest.backup_starting_head, context.current_schema_head
            ));
        }
        for key in STORE_KEYS {
            match manifest.stores.get(key) {
                Some(store) if store.restore_verified.verified => {}
                Some(_) => reasons.push(format!("restore_verified not earned for store: {key}")),
                None => reasons.push(format!("required store missing: {key}")),
            }
        }
        match manifest.stores.get("files") {
            Some(store) if store.archive_verified.verified => {}
            Some(_) => reasons.push("archive_verified not earned for store: files".to_owned()),
            None => reasons.push("required store missing: files".to_owned()),
        }
        reasons.extend(artifact_integrity_errors(
            context.backup_root,
            &manifest.artifacts,
        ));
        if reasons.is_empty() {
            Ok(Self { manifest })
        } else {
            Err(BackupManifestError::from_reasons(reasons))
        }
    }

    pub fn manifest(&self) -> &HubBackupManifest {
        &self.manifest
    }
}

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

fn artifact_integrity_errors(root: &Path, artifacts: &[ArtifactRecord]) -> Vec<String> {
    let mut reasons = Vec::new();
    let canonical_root = match root.canonicalize() {
        Ok(root) => root,
        Err(error) => return vec![format!("backup root is unavailable: {error}")],
    };
    for artifact in artifacts {
        let path = root.join(&artifact.path);
        match fs::symlink_metadata(&path) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                reasons.push(format!("artifact must not be a symlink: {}", artifact.path));
                continue;
            }
            Ok(_) => {}
            Err(error) => {
                reasons.push(format!(
                    "artifact is unavailable {}: {error}",
                    artifact.path
                ));
                continue;
            }
        }
        let canonical = match path.canonicalize() {
            Ok(path) if path.starts_with(&canonical_root) => path,
            Ok(_) => {
                reasons.push(format!("artifact escapes backup root: {}", artifact.path));
                continue;
            }
            Err(error) => {
                reasons.push(format!(
                    "artifact is unavailable {}: {error}",
                    artifact.path
                ));
                continue;
            }
        };
        let metadata = match canonical.metadata() {
            Ok(metadata) if metadata.is_file() => metadata,
            Ok(_) => {
                reasons.push(format!("artifact is not a regular file: {}", artifact.path));
                continue;
            }
            Err(error) => {
                reasons.push(format!(
                    "artifact metadata failed {}: {error}",
                    artifact.path
                ));
                continue;
            }
        };
        if metadata.len() != artifact.size_bytes {
            reasons.push(format!("artifact size mismatch: {}", artifact.path));
        }
        match file_sha256(&canonical) {
            Ok(checksum) if checksum == artifact.sha256 => {}
            Ok(_) => reasons.push(format!("artifact checksum mismatch: {}", artifact.path)),
            Err(error) => reasons.push(format!("artifact read failed {}: {error}", artifact.path)),
        }
    }
    reasons
}

fn file_sha256(path: &Path) -> std::io::Result<String> {
    let mut input = BufReader::new(File::open(path)?);
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = input.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    Ok(digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect())
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

    #[cfg(unix)]
    #[test]
    fn artifact_integrity_rejects_symlinks() -> anyhow::Result<()> {
        use std::os::unix::fs::symlink;

        let root = tempfile::tempdir()?;
        fs::write(root.path().join("target.dump"), [])?;
        symlink("target.dump", root.path().join("linked.dump"))?;
        let artifacts = [ArtifactRecord {
            name: "postgres".to_owned(),
            path: "linked.dump".to_owned(),
            sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855".to_owned(),
            size_bytes: 0,
        }];

        let errors = artifact_integrity_errors(root.path(), &artifacts);
        assert_eq!(errors, ["artifact must not be a symlink: linked.dump"]);
        Ok(())
    }
}

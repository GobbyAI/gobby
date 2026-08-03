use std::fmt::Write as _;
use std::path::Path;

use anyhow::{Context as _, bail};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::config::Context;
use crate::output::{self, Format};
use crate::vector::code_symbols::delete_exact_collection;

const MANIFEST_FORMAT: &str = "gobby-vector-graph-reconcile-deletion";
const RESERVED_NAMESPACES: [&str; 6] = [
    "memories",
    "tool_embeddings",
    "gobby_code",
    "gobby_wiki",
    "gobby_kg",
    "gwiki",
];

pub fn drop_namespace(
    ctx: &Context,
    namespace: &str,
    manifest: &Path,
    manifest_sha256: &str,
    format: Format,
) -> anyhow::Result<()> {
    validate_manifest_target(namespace, manifest, manifest_sha256)?;
    let qdrant = ctx
        .qdrant
        .as_ref()
        .context("drop-namespace requires configured Qdrant")?;
    let deleted = delete_exact_collection(qdrant, namespace)?;

    match format {
        Format::Json => output::print_json(&serde_json::json!({
            "namespace": namespace,
            "deleted": deleted == 1,
        })),
        Format::Text => {
            let message = if deleted == 1 {
                format!("Deleted Qdrant namespace: {namespace}")
            } else {
                format!("Qdrant namespace already absent: {namespace}")
            };
            output::print_text(&message)
        }
    }
}

fn validate_manifest_target(
    namespace: &str,
    manifest: &Path,
    expected_sha256: &str,
) -> anyhow::Result<()> {
    if RESERVED_NAMESPACES.contains(&namespace) {
        bail!("refusing to delete reserved namespace: {namespace}");
    }
    let payload = std::fs::read(manifest)
        .with_context(|| format!("failed to read deletion manifest {}", manifest.display()))?;
    let actual_sha256 = sha256_hex(&payload);
    if !actual_sha256.eq_ignore_ascii_case(expected_sha256) {
        bail!("manifest sha256 mismatch: expected {expected_sha256}, got {actual_sha256}");
    }

    let document: Value = serde_json::from_slice(&payload)
        .with_context(|| format!("invalid deletion manifest JSON: {}", manifest.display()))?;
    if document.get("manifest_format").and_then(Value::as_str) != Some(MANIFEST_FORMAT) {
        bail!("unexpected deletion manifest format");
    }
    if document.get("manifest_version").and_then(Value::as_u64) != Some(1) {
        bail!("unexpected deletion manifest version");
    }
    let deletions = document
        .get("deletions")
        .and_then(Value::as_array)
        .context("deletion manifest has no deletions array")?;
    let matches = deletions
        .iter()
        .filter(|item| {
            item.get("store").and_then(Value::as_str) == Some("qdrant")
                && item.get("namespace").and_then(Value::as_str) == Some(namespace)
                && item.get("tier").and_then(Value::as_u64) == Some(3)
                && item.get("disposition").and_then(Value::as_str) == Some("delete")
                && item.get("owner").and_then(Value::as_str) == Some("gcode-drop-namespace")
        })
        .count();
    if matches != 1 {
        bail!("manifest does not authorize exact Qdrant namespace: {namespace}");
    }
    let inventory_contains_namespace = document
        .pointer("/original_inventory/qdrant")
        .and_then(Value::as_array)
        .is_some_and(|items| items.iter().any(|item| item.as_str() == Some(namespace)));
    if !inventory_contains_namespace {
        bail!("namespace is absent from the manifest's original Qdrant inventory");
    }
    Ok(())
}

fn sha256_hex(payload: &[u8]) -> String {
    let digest = Sha256::digest(payload);
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        let _ = write!(encoded, "{byte:02x}");
    }
    encoded
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write_manifest(namespace: &str) -> (tempfile::TempDir, std::path::PathBuf, String) {
        let directory = tempfile::tempdir().expect("tempdir");
        let path = directory.path().join("deletions.json");
        let document = serde_json::json!({
            "manifest_format": MANIFEST_FORMAT,
            "manifest_version": 1,
            "original_inventory": {"qdrant": [namespace], "falkordb": []},
            "deletions": [{
                "store": "qdrant",
                "namespace": namespace,
                "tier": 3,
                "disposition": "delete",
                "owner": "gcode-drop-namespace",
            }],
        });
        let payload = serde_json::to_vec_pretty(&document).expect("manifest JSON");
        std::fs::write(&path, &payload).expect("write manifest");
        let digest = sha256_hex(&payload);
        (directory, path, digest)
    }

    #[test]
    fn accepts_one_exact_hash_pinned_manifest_target() {
        let namespace = "code_symbols_graph-standalone-123";
        let (_directory, path, digest) = write_manifest(namespace);

        validate_manifest_target(namespace, &path, &digest).expect("manifest authorizes target");
    }

    #[test]
    fn rejects_reserved_namespace_before_manifest_use() {
        let (_directory, path, digest) = write_manifest("memories");

        let error = validate_manifest_target("memories", &path, &digest).expect_err("reserved");

        assert!(error.to_string().contains("reserved"));
    }

    #[test]
    fn rejects_hash_mismatch_and_out_of_manifest_namespace() {
        let (_directory, path, digest) = write_manifest("code_symbols_graph-standalone-123");
        let hash_error =
            validate_manifest_target("code_symbols_graph-standalone-123", &path, &"0".repeat(64))
                .expect_err("hash mismatch");
        assert!(hash_error.to_string().contains("sha256 mismatch"));

        let target_error =
            validate_manifest_target("code_symbols_graph-standalone-456", &path, &digest)
                .expect_err("out of manifest");
        assert!(
            target_error
                .to_string()
                .contains("does not authorize exact")
        );
    }
}

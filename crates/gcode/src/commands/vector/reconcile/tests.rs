use super::*;

#[cfg(gcode_postgres_tests)]
#[path = "tests/serial_db.rs"]
mod serial_db;

/// A private (0700) temp directory under the canonical temp root. macOS temp roots are
/// symlinks (`/var` -> `/private/var`), and `no_symlinks` refuses symlinked ancestors.
/// tempfile's default mode follows the umask (0755 here), which receipt parents refuse.
fn canonical_tempdir() -> anyhow::Result<tempfile::TempDir> {
    use std::os::unix::fs::PermissionsExt;

    Ok(tempfile::Builder::new()
        .permissions(std::fs::Permissions::from_mode(0o700))
        .tempdir_in(std::env::temp_dir().canonicalize()?)?)
}

#[test]
fn apply_refuses_receipt_outside_private_location() {
    use std::os::unix::fs::PermissionsExt;

    use crate::config::{CodeVectorSettings, ProjectIndexScope, QdrantConfig};

    let project_root = canonical_tempdir().expect("project root");
    // The database URL is unreachable: a location refusal must come before any lease attempt.
    let ctx = Context {
        database_url: "postgresql://unreachable.invalid:1/none".to_string(),
        project_root: project_root.path().to_path_buf(),
        project_id: Uuid::new_v4().to_string(),
        quiet: true,
        falkordb: None,
        qdrant: Some(QdrantConfig {
            url: Some("http://127.0.0.1:1".to_string()),
            api_key: None,
        }),
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        machine_id: Uuid::new_v4().to_string(),
        runtime_config_capture_degraded: false,
        indexing: gobby_core::config::IndexingConfig::default(),
        daemon_url: None,
        grant_ai: None,
        index_scope: ProjectIndexScope::Single,
    };
    let digest = format!("sha256:{}", "0".repeat(64));

    let inside_repo = project_root.path().join("receipt.json");
    let error = apply(&ctx, &digest, &inside_repo)
        .expect_err("a receipt inside the repository root is refused");
    assert!(
        error.to_string().contains("repository root"),
        "unexpected refusal: {error:#}",
    );

    let shared_parent = canonical_tempdir().expect("shared parent");
    std::fs::set_permissions(shared_parent.path(), std::fs::Permissions::from_mode(0o755))
        .expect("make parent shared");
    let error = apply(&ctx, &digest, &shared_parent.path().join("receipt.json"))
        .expect_err("a receipt under a non-private parent is refused");
    assert!(
        error.to_string().contains("private"),
        "unexpected refusal: {error:#}",
    );
}

#[test]
fn inventory_digest_is_order_independent_and_content_sensitive() {
    let orphans = vec!["orphan-b".to_string(), "orphan-a".to_string()];
    let synced = vec!["version-2".to_string(), "version-1".to_string()];
    let digest = inventory_digest(&orphans, &synced);

    assert!(digest.starts_with("sha256:"), "digest prefix: {digest}");
    assert_eq!(
        digest.len(),
        "sha256:".len() + 64,
        "digest is a SHA-256 hex"
    );
    assert_eq!(
        digest,
        inventory_digest(
            &["orphan-a".to_string(), "orphan-b".to_string()],
            &["version-1".to_string(), "version-2".to_string()],
        ),
        "digest must not depend on input order"
    );
    assert_ne!(
        digest,
        inventory_digest(&orphans[..1], &synced),
        "removing an orphan must change the digest"
    );
    assert_ne!(
        digest,
        inventory_digest(&orphans, &synced[..1]),
        "removing a synced-missing version must change the digest"
    );
    assert_ne!(
        inventory_digest(&["shared-id".to_string()], &[]),
        inventory_digest(&[], &["shared-id".to_string()]),
        "an ID in the orphan group must not hash like the same ID in the version group"
    );
}

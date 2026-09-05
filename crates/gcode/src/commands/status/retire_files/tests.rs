use super::*;
use manifest::{ContentVersion, RetiredFile};

#[path = "tests/serial_db.rs"]
mod serial_db;

fn sample_manifest(root: &Path) -> Manifest {
    Manifest {
        backends: backend::BackendIdentity {
            postgres_host: "127.0.0.1".to_string(),
            postgres_port: 60893,
            postgres_database: "gobby_gcode_test".to_string(),
            qdrant_url: "http://127.0.0.1:1".to_string(),
            falkor_host: "127.0.0.1".to_string(),
            falkor_port: 1,
            falkor_graph: "gcode_retirement_test".to_string(),
        },
        version: 1,
        source_inventory_digest: "a".repeat(64),
        project_id: uuid::Uuid::new_v4().to_string(),
        machine_id: uuid::Uuid::new_v4().to_string(),
        root_path: root.to_path_buf(),
        files: vec![RetiredFile {
            file_path: "wiki/page.md".to_string(),
            versions: vec![ContentVersion {
                id: uuid::Uuid::new_v4().to_string(),
                content_hash: "b".repeat(64),
                symbol_ids: vec![uuid::Uuid::new_v4().to_string()],
            }],
        }],
    }
}

#[test]
fn manifest_rejects_scope_escapes_and_duplicate_identities() -> anyhow::Result<()> {
    let root = tempfile::tempdir()?;
    let root = root.path().canonicalize()?;
    for path in [
        "",
        "/outside",
        "../outside",
        "wiki/../keep",
        "wiki//page",
        "./wiki/page",
    ] {
        let mut manifest = sample_manifest(&root);
        manifest.files[0].file_path = path.to_string();
        assert!(manifest.validate().is_err(), "accepted {path:?}");
    }
    let mut manifest = sample_manifest(&root);
    let duplicate = manifest.files[0].versions[0].symbol_ids[0].clone();
    manifest.files[0].versions[0].symbol_ids.push(duplicate);
    assert!(manifest.validate().is_err());
    let mut manifest = sample_manifest(&root);
    manifest.files[0].versions[0].content_hash = "not-a-content-identity".to_string();
    assert!(manifest.validate().is_err());
    assert!(sample_manifest(&root).validate().is_ok());
    Ok(())
}

#[test]
fn existing_file_refuses_retirement_readiness() -> anyhow::Result<()> {
    let root = tempfile::tempdir()?;
    let path = root.path().canonicalize()?;
    fs::create_dir(path.join("wiki"))?;
    fs::write(path.join("wiki/page.md"), "retained original")?;
    assert!(manifest::require_absent(&path, "wiki/page.md").is_err());
    assert_eq!(
        fs::read_to_string(path.join("wiki/page.md"))?,
        "retained original"
    );
    assert!(manifest::require_absent(&path, "wiki/missing.md").is_ok());
    Ok(())
}

#[cfg(unix)]
#[test]
fn missing_target_below_symlink_is_refused_without_traversal() -> anyhow::Result<()> {
    use std::os::unix::fs::symlink;
    let root = tempfile::tempdir()?;
    let outside = tempfile::tempdir()?;
    let path = root.path().canonicalize()?;
    symlink(outside.path(), path.join("wiki"))?;
    assert!(manifest::require_absent(&path, "wiki/missing.md").is_err());
    symlink(path.join("nonexistent"), path.join("dangling"))?;
    assert!(manifest::require_absent(&path, "dangling").is_err());
    Ok(())
}

#[cfg(unix)]
#[test]
fn private_manifest_and_receipt_bind_exact_source_and_operation() -> anyhow::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let directory = tempfile::tempdir()?;
    let path = directory.path().canonicalize()?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o700))?;
    let root = path.join("repo");
    fs::create_dir(&root)?;
    let manifest = sample_manifest(&root);
    let manifest_path = path.join("manifest.json");
    fs::write(&manifest_path, serde_json::to_vec(&manifest)?)?;
    fs::set_permissions(&manifest_path, fs::Permissions::from_mode(0o644))?;
    assert!(manifest::read(&manifest_path).is_err());
    fs::set_permissions(&manifest_path, fs::Permissions::from_mode(0o600))?;
    let (_, manifest_digest) = manifest::read(&manifest_path)?;
    let mut receipt = Receipt {
        backends: manifest.backends.clone(),
        version: 1,
        manifest_digest,
        source_inventory_digest: manifest.source_inventory_digest,
        project_id: manifest.project_id,
        machine_id: manifest.machine_id,
        root_path: root.to_string_lossy().into_owned(),
        mode: "apply".to_string(),
        complete: false,
        files: BTreeMap::new(),
    };
    let receipt_path = path.join("receipt.json");
    write_receipt(&receipt_path, &receipt)?;
    assert_eq!(
        fs::metadata(&receipt_path)?.permissions().mode() & 0o777,
        0o600
    );
    let original = fs::read(&receipt_path)?;
    receipt.source_inventory_digest = "c".repeat(64);
    assert!(write_receipt(&receipt_path, &receipt).is_err());
    assert_eq!(fs::read(&receipt_path)?, original);
    Ok(())
}

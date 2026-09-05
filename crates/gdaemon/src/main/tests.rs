use super::*;
use serde_json::{Value, json};

fn manifest(created_at: &str) -> Value {
    let mut manifest: Value = serde_json::from_str(include_str!(
        "../../../gcore/tests/fixtures/hub_backup_manifest/v3_roundtrip.json"
    ))
    .expect("valid shared manifest fixture");
    manifest["created_at"] = json!(created_at);
    manifest
}

fn write_manifest(root: &Path, name: &str, manifest: &Value) -> Result<PathBuf> {
    let path = root.join(name);
    fs::create_dir(&path)?;
    fs::write(
        path.join(BACKUP_MANIFEST_NAME),
        serde_json::to_vec(manifest)?,
    )?;
    Ok(path)
}

#[test]
fn newest_backup_ignores_older_incompatible_store_contract() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?;
    let mut old = manifest("2026-08-15T12:00:00Z");
    old["stores"]
        .as_object_mut()
        .expect("stores")
        .remove("files");
    assert!(parse_backup_manifest(&old.to_string()).is_err());
    write_manifest(&root, "z-old", &old)?;
    let newest = write_manifest(&root, "a-new", &manifest("2026-09-05T12:00:00Z"))?;

    let (selected, parsed) = load_newest_backup_manifest_from_root(&root)?;
    assert_eq!(selected, newest);
    assert!(parsed.stores.contains_key("files"));
    Ok(())
}

#[test]
fn newest_backup_rejects_invalid_newest_without_falling_back() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?;
    write_manifest(&root, "old", &manifest("2026-08-15T12:00:00Z"))?;
    let mut newest = manifest("2026-09-05T12:00:00Z");
    newest["stores"]
        .as_object_mut()
        .expect("stores")
        .remove("files");
    write_manifest(&root, "new", &newest)?;

    let error = load_newest_backup_manifest_from_root(&root).unwrap_err();
    assert!(error.to_string().contains("stores must contain exactly"));
    assert!(error.to_string().contains("new/manifest.json"));
    Ok(())
}

#[test]
fn newest_backup_refuses_unrankable_candidates() -> Result<()> {
    for payload in [
        "{",
        "{}",
        r#"{"created_at":12}"#,
        r#"{"created_at":"invalid"}"#,
    ] {
        let temp = tempfile::tempdir()?;
        let root = temp.path().canonicalize()?;
        write_manifest(&root, "new", &manifest("2026-09-05T12:00:00Z"))?;
        let old = root.join("old");
        fs::create_dir(&old)?;
        fs::write(old.join(BACKUP_MANIFEST_NAME), payload)?;
        assert!(
            load_newest_backup_manifest_from_root(&root).is_err(),
            "{payload}"
        );
    }
    Ok(())
}

#[test]
fn newest_backup_compares_instants_instead_of_timestamp_strings() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?;
    write_manifest(&root, "old", &manifest("2026-09-05T13:00:00+02:00"))?;
    let newest = write_manifest(&root, "new", &manifest("2026-09-05T12:00:00Z"))?;
    assert_eq!(load_newest_backup_manifest_from_root(&root)?.0, newest);
    Ok(())
}

#[cfg(unix)]
#[test]
fn newest_backup_keeps_rejecting_symlink_candidates() -> Result<()> {
    let temp = tempfile::tempdir()?;
    let root = temp.path().canonicalize()?;
    let newest = write_manifest(&root, "new", &manifest("2026-09-05T12:00:00Z"))?;
    std::os::unix::fs::symlink(newest, root.join("linked"))?;
    let error = load_newest_backup_manifest_from_root(&root).unwrap_err();
    assert!(error.to_string().contains("symlink"));
    Ok(())
}

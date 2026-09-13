//! Explicit, caller-local diagnostics. Failures never change a persisted result.

use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::time::{Duration, SystemTime};

pub(crate) fn write_debug(kind: &str, result: &serde_json::Value) {
    match write_bundle(kind, result) {
        Ok(path) => eprintln!("Diagnostic bundle: {}", path.display()),
        Err(error) => eprintln!("Diagnostic write failed (result remains available): {error:#}"),
    }
}

fn write_bundle(kind: &str, result: &serde_json::Value) -> anyhow::Result<std::path::PathBuf> {
    let root = gobby_core::gobby_home()?.join("ask-debug");
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&root)?;
    let days: u64 = std::env::var("GOBBY_ASK_RETENTION_DAYS")
        .unwrap_or_else(|_| "7".to_string())
        .parse()?;
    anyhow::ensure!(
        (1..=3650).contains(&days),
        "Ask retention must be 1..3650 days"
    );
    let retention = Duration::from_secs(days * 86400);
    for entry in std::fs::read_dir(&root)?.take(1000) {
        let entry = entry?;
        if entry.file_type()?.is_dir()
            && entry.file_name().to_string_lossy().starts_with("bundle-")
            && entry.metadata()?.modified()?.elapsed().unwrap_or_default() > retention
        {
            std::fs::remove_dir_all(entry.path())?;
        }
    }
    let timestamp = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)?
        .as_nanos();
    let directory = root.join(format!("bundle-{timestamp}-{}", std::process::id()));
    std::fs::DirBuilder::new().mode(0o700).create(&directory)?;
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(directory.join(format!("{kind}.json")))?;
    file.write_all(&serde_json::to_vec_pretty(result)?)?;
    file.sync_all()?;
    Ok(directory)
}

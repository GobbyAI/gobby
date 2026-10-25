use std::path::Path;
use std::process::Command;

pub(super) fn write_file(root: &Path, rel: &str, contents: &[u8]) {
    let path = root.join(rel);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).expect("create parent");
    }
    std::fs::write(path, contents).expect("write file");
}

/// Run git in `dir` with a fixed test identity, signing off, and an empty
/// hooks directory so the operator's global hooks never run.
pub(super) fn git(dir: &Path, hooks: &Path, args: &[&str]) {
    git_output(dir, hooks, args);
}

pub(super) fn git_output(dir: &Path, hooks: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args([
            "-c",
            "user.name=Gcode Test",
            "-c",
            "user.email=gcode@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
        ])
        .arg(format!("core.hooksPath={}", hooks.display()))
        .args(args)
        .output()
        .expect("run git");
    assert!(
        output.status.success(),
        "git {args:?} failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

/// Writes the overlay's isolation marker with complete pin provenance: the
/// parent it branched from and the base commit its pins were taken at.
pub(super) fn write_isolation_marker(
    overlay_root: &Path,
    parent_root: &Path,
    parent_project_id: &str,
    base_commit: &str,
) {
    let dir = overlay_root.join(".gobby");
    std::fs::create_dir_all(&dir).expect("create gobby dir");
    let parent = parent_root.to_str().expect("utf-8 parent path");
    let marker = serde_json::json!({
        "parent_project_path": parent,
        "parent_project_id": parent_project_id,
        "base_commit": base_commit,
        "ancestor_checkout_path": parent,
        "ancestor_checkout_identity": parent_project_id,
    });
    std::fs::write(dir.join("isolation.json"), marker.to_string()).expect("write isolation marker");
}

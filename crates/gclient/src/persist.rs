//! Local JSON workspace snapshot per project.

use serde::{Deserialize, Serialize};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkspaceSnapshot {
    pub project_id: String,
    pub terminal_ids: Vec<String>,
    pub focus: Option<String>,
}

pub fn snapshot_path(gobby_home: &Path, project_id: &str) -> PathBuf {
    gobby_home
        .join("client")
        .join(project_id)
        .join("workspace.json")
}

pub fn save_snapshot(gobby_home: &Path, snapshot: &WorkspaceSnapshot) -> io::Result<PathBuf> {
    let path = snapshot_path(gobby_home, &snapshot.project_id);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    fs::write(
        &tmp,
        serde_json::to_vec_pretty(snapshot).map_err(io::Error::other)?,
    )?;
    fs::rename(&tmp, &path)?;
    Ok(path)
}

pub fn load_snapshot(gobby_home: &Path, project_id: &str) -> io::Result<WorkspaceSnapshot> {
    let path = snapshot_path(gobby_home, project_id);
    let bytes = fs::read(path)?;
    serde_json::from_slice(&bytes).map_err(io::Error::other)
}

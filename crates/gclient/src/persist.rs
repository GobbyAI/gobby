//! Local JSON workspace snapshot per project.

use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};
use uuid::Uuid;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SplitAxis {
    Horizontal,
    Vertical,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum LayoutNode {
    Empty,
    Pane {
        terminal_id: String,
    },
    Split {
        axis: SplitAxis,
        children: Vec<LayoutNode>,
    },
}

impl LayoutNode {
    fn collect_terminal_ids(&self, ids: &mut Vec<String>) {
        match self {
            Self::Empty => {}
            Self::Pane { terminal_id } => {
                if !ids.contains(terminal_id) {
                    ids.push(terminal_id.clone());
                }
            }
            Self::Split { children, .. } => {
                for child in children {
                    child.collect_terminal_ids(ids);
                }
            }
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkspaceSnapshot {
    pub project_id: String,
    pub layout: LayoutNode,
    pub tab_order: Vec<String>,
    pub focused_terminal_id: Option<String>,
}

impl WorkspaceSnapshot {
    pub fn empty(project_id: impl Into<String>) -> Self {
        Self {
            project_id: project_id.into(),
            layout: LayoutNode::Empty,
            tab_order: Vec::new(),
            focused_terminal_id: None,
        }
    }

    pub fn terminal_ids(&self) -> Vec<String> {
        let mut ids = Vec::new();
        self.layout.collect_terminal_ids(&mut ids);
        for terminal_id in &self.tab_order {
            if !ids.contains(terminal_id) {
                ids.push(terminal_id.clone());
            }
        }
        ids
    }
}

pub fn snapshot_path(gobby_home: &Path, project_id: &str) -> PathBuf {
    gobby_home
        .join("client")
        .join(project_id)
        .join("workspace.json")
}

pub fn save_snapshot(gobby_home: &Path, snapshot: &WorkspaceSnapshot) -> io::Result<PathBuf> {
    let path = snapshot_path(gobby_home, &snapshot.project_id);
    let bytes = serde_json::to_vec_pretty(snapshot).map_err(io::Error::other)?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::other("workspace snapshot has no parent directory"))?;
    let tmp = parent.join(format!(".workspace.json.{}.tmp", Uuid::new_v4()));
    let result = (|| {
        let mut file = OpenOptions::new().write(true).create_new(true).open(&tmp)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        fs::rename(&tmp, &path)
    })();
    if let Err(error) = result {
        let _ = fs::remove_file(&tmp);
        return Err(error);
    }
    Ok(path)
}

pub fn load_snapshot(gobby_home: &Path, project_id: &str) -> io::Result<WorkspaceSnapshot> {
    let path = snapshot_path(gobby_home, project_id);
    let bytes = fs::read(&path)?;
    match serde_json::from_slice(&bytes) {
        Ok(snapshot) => Ok(snapshot),
        Err(error) => {
            let timestamp = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis();
            let corrupt = path.with_file_name(format!("workspace.json.corrupt-{timestamp}"));
            fs::rename(&path, &corrupt)?;
            tracing::error!(
                path = %path.display(),
                quarantine = %corrupt.display(),
                %error,
                "quarantined corrupt gclient workspace snapshot"
            );
            Ok(WorkspaceSnapshot::empty(project_id))
        }
    }
}

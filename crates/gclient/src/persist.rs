//! Local JSON state: the workspace snapshot per project and the client-wide
//! session file.

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

/// One tab's BSP tree with its slots named by terminal id.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum LayoutNode {
    Empty,
    Pane {
        terminal_id: String,
    },
    Split {
        axis: SplitAxis,
        #[serde(default = "even_ratio")]
        ratio: f32,
        children: Vec<LayoutNode>,
    },
}

fn even_ratio() -> f32 {
    0.5
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

/// One tab of the snapshot; `focused` names the terminal in its focused slot
/// and `worktree_id` the worktree a worktree row opened it in.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TabSnapshot {
    pub title: String,
    pub layout: LayoutNode,
    pub focused: Option<String>,
    #[serde(default)]
    pub worktree_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WorkspaceSnapshot {
    pub project_id: String,
    pub tabs: Vec<TabSnapshot>,
    pub active_tab: usize,
    pub focused_terminal_id: Option<String>,
}

impl WorkspaceSnapshot {
    pub fn empty(project_id: impl Into<String>) -> Self {
        Self {
            project_id: project_id.into(),
            tabs: Vec::new(),
            active_tab: 0,
            focused_terminal_id: None,
        }
    }

    /// Every terminal the snapshot shows, tab by tab in layout order.
    pub fn terminal_ids(&self) -> Vec<String> {
        let mut ids = Vec::new();
        for tab in &self.tabs {
            tab.layout.collect_terminal_ids(&mut ids);
        }
        ids
    }
}

/// The sidebar state that outlives a project focus.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SidebarSnapshot {
    pub collapsed: bool,
    pub width: u16,
    /// The projects section lists every project instead of the working ones.
    #[serde(default)]
    pub all_projects: bool,
    /// The sessions section lists every project's rows instead of the
    /// focused project's.
    #[serde(default)]
    pub all_sessions: bool,
    pub machine_filter: Option<String>,
    /// Project ids in the order the user dragged them into.
    #[serde(default)]
    pub project_order: Vec<String>,
    /// Labels the user gave project cards, by project id.
    #[serde(default)]
    pub project_labels: std::collections::BTreeMap<String, String>,
}

/// Client-wide state: `~/.gobby/client/session.json`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClientSession {
    pub focused_project: Option<String>,
    pub sidebar: SidebarSnapshot,
}

pub fn snapshot_path(gobby_home: &Path, project_id: &str) -> PathBuf {
    gobby_home
        .join("client")
        .join(project_id)
        .join("workspace.json")
}

pub fn session_path(gobby_home: &Path) -> PathBuf {
    gobby_home.join("client").join("session.json")
}

pub fn save_snapshot(gobby_home: &Path, snapshot: &WorkspaceSnapshot) -> io::Result<PathBuf> {
    let path = snapshot_path(gobby_home, &snapshot.project_id);
    write_json(&path, snapshot)?;
    Ok(path)
}

pub fn save_session(gobby_home: &Path, session: &ClientSession) -> io::Result<PathBuf> {
    let path = session_path(gobby_home);
    write_json(&path, session)?;
    Ok(path)
}

/// Serialize into a fresh temp file beside `path` and rename it over, so a
/// reader sees the old file or the new one and never a partial write.
fn write_json<T: Serialize>(path: &Path, value: &T) -> io::Result<()> {
    let bytes = serde_json::to_vec_pretty(value).map_err(io::Error::other)?;
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::other("state file has no parent directory"))?;
    fs::create_dir_all(parent)?;
    let tmp = parent.join(format!(".{}.{}.tmp", file_name(path), Uuid::new_v4()));
    let result = (|| {
        let mut file = OpenOptions::new().write(true).create_new(true).open(&tmp)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        fs::rename(&tmp, path)
    })();
    if let Err(error) = result {
        let _ = fs::remove_file(&tmp);
        return Err(error);
    }
    Ok(())
}

pub fn load_snapshot(gobby_home: &Path, project_id: &str) -> io::Result<WorkspaceSnapshot> {
    let path = snapshot_path(gobby_home, project_id);
    let bytes = fs::read(&path)?;
    match serde_json::from_slice(&bytes) {
        Ok(snapshot) => Ok(snapshot),
        Err(error) => {
            quarantine(&path, &error)?;
            Ok(WorkspaceSnapshot::empty(project_id))
        }
    }
}

/// `None` before a session was saved; a corrupt file is quarantined and also
/// reads as none.
pub fn load_session(gobby_home: &Path) -> io::Result<Option<ClientSession>> {
    let path = session_path(gobby_home);
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    match serde_json::from_slice(&bytes) {
        Ok(session) => Ok(Some(session)),
        Err(error) => {
            quarantine(&path, &error)?;
            Ok(None)
        }
    }
}

fn quarantine(path: &Path, error: &serde_json::Error) -> io::Result<()> {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let corrupt = path.with_file_name(format!("{}.corrupt-{timestamp}", file_name(path)));
    fs::rename(path, &corrupt)?;
    tracing::error!(
        path = %path.display(),
        quarantine = %corrupt.display(),
        %error,
        "quarantined corrupt gclient state file"
    );
    Ok(())
}

fn file_name(path: &Path) -> &str {
    path.file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("state.json")
}

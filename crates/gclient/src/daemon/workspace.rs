//! Workspace rows, ops, and events on the terminal WebSocket (plan
//! gclient-workspaces 4.1), and the scripted daemon's workspace arms.
//!
//! The shapes mirror `src/gobby/servers/websocket/workspace_ws.py`: rows are the
//! storage rows as JSON, an op is `<scope>.<verb>` carrying the parameters of the
//! `WorkspaceOps` method of that name, and replies correlate on `request_id`.

use super::{DaemonError, ScriptedDaemon, Snapshot};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LayoutAxis {
    Horizontal,
    Vertical,
}

/// A tab's split tree as the daemon stores it: leaves name pane ids and every
/// split has exactly two children.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum LayoutNode {
    Pane {
        pane_id: String,
    },
    Split {
        axis: LayoutAxis,
        ratio: f64,
        children: Box<[LayoutNode; 2]>,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkspaceRow {
    pub id: String,
    pub machine_id: String,
    #[serde(rename = "ref")]
    pub reference: u64,
    pub name: String,
    pub focused_project_id: Option<String>,
    pub focused_tab_id: Option<String>,
    /// The project this workspace opens for. Absent on a projectless scratch.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub default_project_id: Option<String>,
    pub created_at: String,
    pub updated_at: String,
    /// The owning node's ref; only a `workspace_snapshot` reply carries it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub node_ref: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TabRow {
    pub id: String,
    pub workspace_id: String,
    #[serde(rename = "ref")]
    pub reference: u64,
    pub title: Option<String>,
    pub project_id: String,
    pub worktree_id: Option<String>,
    pub position: u32,
    pub focused_pane_id: Option<String>,
    pub layout: LayoutNode,
    pub created_at: String,
    pub updated_at: String,
}

/// One pane row; `terminal_id` is `None` while its spawn is in flight.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaneRow {
    pub id: String,
    pub tab_id: String,
    #[serde(rename = "ref")]
    pub reference: u64,
    pub terminal_id: Option<String>,
    pub owns_terminal: bool,
    pub label: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

/// A `workspace_snapshot` reply: one workspace's rows under the lifecycle watermark.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WorkspaceSnapshot {
    pub workspace: WorkspaceRow,
    pub tabs: Vec<TabRow>,
    pub panes: Vec<PaneRow>,
    pub snapshot: Snapshot,
}

/// A `workspace_op` reply: the op's return value as JSON.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WorkspaceReply {
    pub op: String,
    pub result: Value,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkspaceErrorCode {
    NotFound,
    InvalidRef,
    InvalidOp,
    TerminalFailed,
    Busy,
    Forbidden,
}

/// A `workspace_error` reply.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkspaceError {
    pub code: WorkspaceErrorCode,
    pub reason: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum WorkspaceEventKind {
    #[serde(rename = "workspace.created")]
    WorkspaceCreated,
    #[serde(rename = "workspace.renamed")]
    WorkspaceRenamed,
    #[serde(rename = "workspace.closed")]
    WorkspaceClosed,
    #[serde(rename = "tab.created")]
    TabCreated,
    #[serde(rename = "tab.renamed")]
    TabRenamed,
    #[serde(rename = "tab.moved")]
    TabMoved,
    #[serde(rename = "tab.closed")]
    TabClosed,
    #[serde(rename = "tab.removed")]
    TabRemoved,
    #[serde(rename = "pane.added")]
    PaneAdded,
    #[serde(rename = "pane.swapped")]
    PaneSwapped,
    #[serde(rename = "pane.moved")]
    PaneMoved,
    #[serde(rename = "pane.resized")]
    PaneResized,
    #[serde(rename = "pane.renamed")]
    PaneRenamed,
    #[serde(rename = "pane.removed")]
    PaneRemoved,
    #[serde(rename = "focus_hints")]
    FocusHints,
}

/// A `workspace_event`: the rows one mutation changed, in the lifecycle order
/// shared with `terminal_event`. `pane.removed`, `tab.removed`, `tab.closed`,
/// and `workspace.closed` carry the rows that went away.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WorkspaceEvent {
    pub kind: WorkspaceEventKind,
    pub workspace_id: String,
    /// The one project the event's tabs name, else `None`.
    pub project_id: Option<String>,
    pub workspace: Option<WorkspaceRow>,
    pub tabs: Vec<TabRow>,
    pub panes: Vec<PaneRow>,
    pub daemon_epoch: String,
    #[serde(deserialize_with = "super::ws::deserialize_safe_u64")]
    pub seq: u64,
    pub timestamp: String,
}

/// One `workspace_op`. Fields are the `WorkspaceOps` method's parameters after
/// the actor: an `Option` that is omitted when `None` takes the op's default,
/// and the nullable parameters without a default (`title`, `label`, and the
/// focus hints) are always sent.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "op")]
pub enum WorkspaceOp {
    #[serde(rename = "workspace.list")]
    WorkspaceList {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "workspace.create")]
    WorkspaceCreate {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        name: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "workspace.rename")]
    WorkspaceRename {
        workspace: String,
        name: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "workspace.close")]
    WorkspaceClose {
        workspace: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "workspace.set_focus_hints")]
    WorkspaceSetFocusHints {
        workspace: String,
        project_id: Option<String>,
        tab: Option<String>,
        pane: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "tab.create")]
    TabCreate {
        workspace: String,
        project_id: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        worktree_id: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        title: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        terminal_id: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "tab.rename")]
    TabRename {
        tab: String,
        title: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "tab.move")]
    TabMove {
        tab: String,
        position: u32,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        workspace: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "tab.close")]
    TabClose {
        tab: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "pane.split")]
    PaneSplit {
        pane: String,
        axis: LayoutAxis,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        terminal_id: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "pane.swap")]
    PaneSwap {
        pane: String,
        other: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "pane.move")]
    PaneMove {
        pane: String,
        tab: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        beside: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        axis: Option<LayoutAxis>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "pane.resize")]
    PaneResize {
        pane: String,
        ratio: f64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "pane.rename")]
    PaneRename {
        pane: String,
        label: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "pane.close")]
    PaneClose {
        pane: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "pane.send_text")]
    PaneSendText {
        pane: String,
        text: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        submit: Option<bool>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        idempotency_key: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "pane.send_keys")]
    PaneSendKeys {
        pane: String,
        keys: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        literal: Option<bool>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        idempotency_key: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "pane.read")]
    PaneRead {
        pane: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        lines: Option<u32>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
    #[serde(rename = "pane.wait_for_output")]
    PaneWaitForOutput {
        pane: String,
        pattern: String,
        timeout_seconds: f64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        poll_interval_seconds: Option<f64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        node: Option<String>,
    },
}

/// A `workspace_attach` request without its `request_id`; absent references
/// are omitted so the daemon picks the local node and its default workspace.
pub(super) fn attach_request(
    node: Option<&str>,
    workspace: Option<&str>,
    project_id: Option<&str>,
) -> Value {
    let mut request = json!({"type": "workspace_attach"});
    for (field, value) in [
        ("node", node),
        ("workspace", workspace),
        ("project_id", project_id),
    ] {
        if let Some(value) = value {
            request[field] = json!(value);
        }
    }
    request
}

/// A `workspace_op` request without its `request_id`.
pub(super) fn op_request(op: &WorkspaceOp) -> Result<Value, DaemonError> {
    let mut request = serde_json::to_value(op).map_err(|error| DaemonError::Protocol {
        detail: error.to_string(),
    })?;
    request["type"] = json!("workspace_op");
    Ok(request)
}

impl ScriptedDaemon {
    /// Seed the workspace that `attach_workspace` serves.
    pub fn set_workspace(&self, snapshot: WorkspaceSnapshot) {
        self.state().workspace = Some(snapshot);
    }

    pub(super) fn attach_workspace_scripted(
        &self,
        node: Option<&str>,
        workspace: Option<&str>,
        project_id: Option<&str>,
    ) -> Result<WorkspaceSnapshot, DaemonError> {
        self.send_ws(attach_request(node, workspace, project_id))?;
        self.state().workspace.clone().ok_or(DaemonError::NotFound)
    }

    pub(super) fn workspace_op_scripted(
        &self,
        op: WorkspaceOp,
    ) -> Result<WorkspaceReply, DaemonError> {
        let request = op_request(&op)?;
        let name = request["op"].as_str().unwrap_or_default().to_string();
        self.send_ws(request)?;
        Ok(WorkspaceReply {
            op: name,
            result: Value::Null,
        })
    }
}

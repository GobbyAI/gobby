//! Reach every workspace on the node without leaving the window.
//!
//! `prefix+shift+n` stays the new-project chord. This cycle is the switcher:
//! it walks the node's workspaces in ref order and attaches the next one.

use crate::daemon::{Daemon, DaemonError, LiveDaemon, WorkspaceOp};
use crate::frame_source::FrameError;
use crate::ui::status::Toast;
use crate::ui::Chrome;

use super::super::Workspace;

pub(super) async fn switch_next_workspace(
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<(), FrameError> {
    let node = workspace.attach_target().node.clone();
    let reply = workspace
        .daemon()
        .workspace_op(WorkspaceOp::WorkspaceList { node: node.clone() })
        .await?;
    let rows = reply
        .result
        .as_array()
        .cloned()
        .ok_or_else(|| DaemonError::Protocol {
            detail: "workspace.list did not return a list".to_string(),
        })?;
    if rows.is_empty() {
        chrome.notify(Toast::info("This node has no workspace"));
        return Ok(());
    }
    let current = workspace
        .workspace_model()
        .map(|model| model.workspace.id.clone());
    let index = rows
        .iter()
        .position(|row| row.get("id").and_then(|id| id.as_str()) == current.as_deref())
        .unwrap_or(0);
    let next = &rows[(index + 1) % rows.len()];
    let Some(id) = next.get("id").and_then(|value| value.as_str()) else {
        return Err(DaemonError::Protocol {
            detail: "workspace row has no id".to_string(),
        }
        .into());
    };
    let snapshot = workspace
        .daemon()
        .attach_workspace(node.as_deref(), Some(id), None)
        .await?;
    workspace.apply_workspace_snapshot(snapshot);
    let mut target = workspace.attach_target().clone();
    target.workspace = Some(id.to_owned());
    target.project_id = None;
    workspace.set_attach_target(target);
    let reference = next
        .get("ref")
        .and_then(|value| value.as_u64())
        .unwrap_or(0);
    let name = next
        .get("name")
        .and_then(|value| value.as_str())
        .unwrap_or(id);
    chrome.notify(Toast::info(format!("Workspace {reference} {name}")));
    Ok(())
}

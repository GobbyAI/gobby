//! Remote terminal placement lookup, performed exclusively by a navigation job.

use super::project_navigation::Prepared;
use crate::daemon::{Daemon, DaemonError, Generation, LiveDaemon, WorkspaceErrorCode, WorkspaceOp};

pub(super) struct Selection<'a> {
    pub terminal: &'a str,
    pub known: bool,
    pub attention: bool,
    pub reuse: bool,
}

pub(super) async fn prepare(
    daemon: &LiveDaemon,
    generation: Generation,
    node: Option<&str>,
    selection: Selection<'_>,
    roster: impl std::future::Future<Output = Result<super::super::live::relist::Relist, DaemonError>>,
) -> Result<Prepared, String> {
    let Selection {
        terminal,
        known,
        attention,
        reuse,
    } = selection;
    let reply = daemon
        .workspace_op(WorkspaceOp::WorkspaceList {
            node: node.map(str::to_owned),
        })
        .await
        .map_err(|error| error.to_string())?;
    let rows = reply.result.as_array().ok_or("Invalid workspace list")?;
    for row in rows {
        let id = row
            .get("id")
            .and_then(|id| id.as_str())
            .ok_or("Invalid workspace row")?;
        let snapshot = match daemon.workspace_snapshot(node, id).await {
            Ok(snapshot) => snapshot,
            Err(DaemonError::Workspace(error)) if error.code == WorkspaceErrorCode::NotFound => {
                continue
            }
            Err(error) => return Err(error.to_string()),
        };
        let project = snapshot
            .panes
            .iter()
            .find(|pane| pane.terminal_id.as_deref() == Some(terminal))
            .and_then(|pane| snapshot.tabs.iter().find(|tab| tab.id == pane.tab_id))
            .map(|tab| tab.project_id.clone());
        let Some(project) = project else {
            continue;
        };
        let attached = daemon
            .attach_workspace_at_generation(node, Some(id), None, generation)
            .await
            .map_err(|error| error.to_string())?;
        if !attached
            .panes
            .iter()
            .any(|pane| pane.terminal_id.as_deref() == Some(terminal))
        {
            return Err("Terminal moved; select it again".into());
        }
        let row = if known {
            None
        } else {
            Some(
                daemon
                    .terminal(terminal)
                    .await
                    .map_err(|error| error.to_string())?,
            )
        };
        return Ok(Prepared {
            snapshot: Some(attached),
            project: Some(project),
            terminal: row,
            terminal_id: Some(terminal.to_owned()),
            ..Prepared::default()
        });
    }
    if known && reuse {
        return Ok(Prepared {
            terminal_id: Some(terminal.to_owned()),
            ..Prepared::default()
        });
    }
    // An unplaced terminal can still be explicitly selected from the roster.
    let roster = roster.await.map_err(|error| error.to_string())?;
    if !roster.contains_terminal(terminal) && !attention {
        return Ok(Prepared {
            roster: Some(roster),
            missing: true,
            ..Prepared::default()
        });
    }
    let row = if known {
        None
    } else {
        Some(
            daemon
                .terminal(terminal)
                .await
                .map_err(|error| error.to_string())?,
        )
    };
    Ok(Prepared {
        roster: Some(roster),
        terminal: row,
        terminal_id: Some(terminal.to_owned()),
        ..Prepared::default()
    })
}

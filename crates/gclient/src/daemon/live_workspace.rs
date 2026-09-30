//! Workspace requests on the live daemon (plan gclient-workspaces 4.1).

use super::live::LiveDaemon;
use super::workspace::{attach_request, op_request};
use super::{DaemonError, WorkspaceOp, WorkspaceReply, WorkspaceSnapshot};
use gobby_terminal::terminal_theme::ThemeDeclaration;
use serde::de::DeserializeOwned;
use serde_json::{json, Value};
use std::time::Duration;
use uuid::Uuid;

impl LiveDaemon {
    pub(super) async fn attach_workspace_live(
        &self,
        node: Option<&str>,
        workspace: Option<&str>,
        project_id: Option<&str>,
    ) -> Result<WorkspaceSnapshot, DaemonError> {
        let request_id = Uuid::new_v4().to_string();
        let mut request = attach_request(node, workspace, project_id);
        request["request_id"] = json!(request_id);
        // The reader marks the attached workspace when it routes this request's
        // reply, before it reads the next frame, so no event slips past its filter.
        self.inner
            .state()
            .workspace_attaches
            .insert(request_id.clone());
        let _pending = PendingAttach {
            daemon: self,
            request_id,
        };
        decode(self.request(request).await?)
    }

    /// Read another workspace without changing this socket's event subscription.
    pub async fn workspace_snapshot(
        &self,
        node: Option<&str>,
        workspace: &str,
    ) -> Result<WorkspaceSnapshot, DaemonError> {
        let mut request = attach_request(node, Some(workspace), None);
        request["type"] = json!("workspace_snapshot");
        request["request_id"] = json!(Uuid::new_v4().to_string());
        decode(self.request(request).await?)
    }

    pub(super) async fn workspace_op_live(
        &self,
        op: WorkspaceOp,
    ) -> Result<WorkspaceReply, DaemonError> {
        let mut request = op_request(&op)?;
        request["request_id"] = json!(Uuid::new_v4().to_string());
        self.theme_spawn(&op, &mut request);
        decode(self.request(request).await?)
    }

    /// Record the client's terminal colours for the shell spawns this
    /// connection requests from now on.
    pub fn set_terminal_theme(&self, theme: &ThemeDeclaration) {
        let mut state = self.inner.state();
        if state.terminal_theme.as_ref() != Some(theme) {
            state.terminal_theme = Some(theme.clone());
        }
    }

    /// Add the client's colours to a request that spawns a shell.
    pub(super) fn theme_spawn(&self, op: &WorkspaceOp, request: &mut Value) {
        if !matches!(
            op,
            WorkspaceOp::TabCreate { .. } | WorkspaceOp::PaneSplit { .. }
        ) {
            return;
        }
        self.theme_request(request);
    }

    pub(super) fn theme_request(&self, request: &mut Value) {
        if let Some(theme) = self.inner.state().terminal_theme.as_ref() {
            request["terminal_theme"] = json!(theme);
        }
    }

    /// A one-shot wait may legitimately outlast the normal workspace request limit.
    pub async fn workspace_op_with_deadline(
        &self,
        op: WorkspaceOp,
        deadline: Duration,
    ) -> Result<WorkspaceReply, DaemonError> {
        let mut request = op_request(&op)?;
        request["request_id"] = json!(Uuid::new_v4().to_string());
        self.theme_spawn(&op, &mut request);
        decode(self.request_with_deadline(request, Some(deadline)).await?)
    }
}

/// Forgets an attach's request id however the request ends, including
/// cancellation, so a late reply cannot retarget the reader's filter.
struct PendingAttach<'a> {
    daemon: &'a LiveDaemon,
    request_id: String,
}

impl Drop for PendingAttach<'_> {
    fn drop(&mut self) {
        self.daemon
            .inner
            .state()
            .workspace_attaches
            .remove(&self.request_id);
    }
}

fn decode<T: DeserializeOwned>(reply: Value) -> Result<T, DaemonError> {
    serde_json::from_value(reply).map_err(|error| DaemonError::Protocol {
        detail: error.to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::daemon::LayoutAxis;
    use gobby_terminal::terminal_theme::RgbColor;

    fn split() -> WorkspaceOp {
        WorkspaceOp::PaneSplit {
            pane: "pane-1".into(),
            axis: LayoutAxis::Horizontal,
            terminal_id: None,
            cwd: None,
            node: None,
        }
    }

    fn themed(daemon: &LiveDaemon, op: &WorkspaceOp) -> Value {
        let mut request = op_request(op).expect("request");
        daemon.theme_spawn(op, &mut request);
        request
    }

    #[test]
    fn only_shell_spawning_ops_carry_the_client_theme() {
        let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "token").expect("daemon");
        assert!(themed(&daemon, &split()).get("terminal_theme").is_none());

        let theme = ThemeDeclaration {
            background: Some(RgbColor {
                r: 0xfa,
                g: 0xfb,
                b: 0xfc,
            }),
            ..ThemeDeclaration::default()
        };
        daemon.set_terminal_theme(&theme);
        let create = WorkspaceOp::TabCreate {
            workspace: "workspace-1".into(),
            project_id: "project-1".into(),
            worktree_id: None,
            title: None,
            terminal_id: None,
            cwd: None,
            node: None,
        };
        for op in [split(), create] {
            assert_eq!(themed(&daemon, &op)["terminal_theme"], json!(theme));
        }
        // The daemon refuses a field an op does not take.
        let close = WorkspaceOp::TabClose {
            tab: "tab-1".into(),
            node: None,
        };
        assert!(themed(&daemon, &close).get("terminal_theme").is_none());
    }
}

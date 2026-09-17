//! Workspace requests on the live daemon (plan gclient-workspaces 4.1).

use super::live::LiveDaemon;
use super::workspace::{attach_request, op_request};
use super::{DaemonError, WorkspaceOp, WorkspaceReply, WorkspaceSnapshot};
use serde::de::DeserializeOwned;
use serde_json::{json, Value};
use uuid::Uuid;

impl LiveDaemon {
    pub(super) async fn attach_workspace_live(
        &self,
        node: Option<&str>,
        workspace: Option<&str>,
    ) -> Result<WorkspaceSnapshot, DaemonError> {
        let request_id = Uuid::new_v4().to_string();
        let mut request = attach_request(node, workspace);
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

    pub(super) async fn workspace_op_live(
        &self,
        op: WorkspaceOp,
    ) -> Result<WorkspaceReply, DaemonError> {
        let mut request = op_request(&op)?;
        request["request_id"] = json!(Uuid::new_v4().to_string());
        decode(self.request(request).await?)
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

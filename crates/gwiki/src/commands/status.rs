use serde_json::json;

use crate::{CommandOutcome, ScopeIdentity, ScopeSelection};

pub(crate) fn execute(selection: ScopeSelection) -> CommandOutcome {
    render(selection.identity())
}

fn render(scope: ScopeIdentity) -> CommandOutcome {
    let daemon_url = gobby_core::daemon_url::daemon_url();
    let payload = json!({
        "command": "status",
        "scope": scope,
        "status": "shell-ready",
        "daemon_url": daemon_url,
    });
    let text = format!(
        "gwiki shell ready
Scope: {scope}
Daemon: {daemon_url}"
    );
    super::scoped_outcome("status", &scope, payload, text)
}

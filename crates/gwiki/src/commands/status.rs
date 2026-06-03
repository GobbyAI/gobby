use serde_json::json;

use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    Ok(render(resolved_scope_identity(&scope)))
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

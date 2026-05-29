use serde_json::json;

use crate::CommandOutcome;
use crate::ScopeIdentity;

pub fn run(scope: ScopeIdentity) -> CommandOutcome {
    let daemon_url = gobby_core::daemon_url::daemon_url();
    let payload = json!({
        "command": "status",
        "scope": scope,
        "status": "ready",
        "daemon_url": daemon_url,
    });
    let text = format!("gwiki shell ready\nScope: {scope}\nDaemon: {daemon_url}");
    super::scoped_outcome("status", &scope, payload, text)
}

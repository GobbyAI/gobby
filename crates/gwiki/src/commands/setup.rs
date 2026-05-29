use serde_json::json;

use crate::CommandOutcome;
use crate::ScopeIdentity;

pub fn run(scope: ScopeIdentity) -> CommandOutcome {
    let payload = json!({
        "command": "setup",
        "scope": scope,
        "status": "ready",
        "objects": [],
    });
    let text = format!("Setup ready\nScope: {scope}");
    super::scoped_outcome("setup", &scope, payload, text)
}

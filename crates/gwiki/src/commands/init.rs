use serde_json::json;

use crate::CommandOutcome;
use crate::ScopeIdentity;

pub fn run(scope: ScopeIdentity) -> CommandOutcome {
    let payload = json!({
        "command": "init",
        "scope": scope,
        "status": "ready",
        "created": [],
    });
    let text = format!("Init ready\nScope: {scope}");
    super::scoped_outcome("init", &scope, payload, text)
}

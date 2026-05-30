use serde_json::json;

use crate::CommandOutcome;
use crate::ScopeIdentity;

pub(crate) fn run(
    scope: ScopeIdentity,
    status: &str,
    objects: Vec<serde_json::Value>,
    created: Vec<String>,
    skipped: Vec<String>,
    failed: Vec<(String, String)>,
    ownership: &str,
) -> CommandOutcome {
    let object_count = objects.len();
    let payload = json!({
        "command": "setup",
        "scope": scope,
        "status": status,
        "objects": objects,
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "ownership": ownership,
    });
    let text = format!("Setup {status}\nScope: {scope}\nObjects: {object_count}");
    super::scoped_outcome("setup", &scope, payload, text)
}

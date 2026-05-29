use serde_json::json;

use crate::CommandOutcome;
use crate::ScopeIdentity;

pub fn run(page: String, scope: ScopeIdentity) -> CommandOutcome {
    let payload = json!({
        "command": "backlinks",
        "scope": scope,
        "page": page,
        "backlinks": [],
    });
    let text = format!("Backlinks for {page}\nScope: {scope}\nNo backlinks");
    super::scoped_outcome("backlinks", &scope, payload, text)
}

pub fn link_suggest(scope: ScopeIdentity, limit: usize) -> CommandOutcome {
    let payload = json!({
        "command": "link-suggest",
        "scope": scope,
        "limit": limit,
        "suggestions": [],
    });
    let text = format!("Link suggestions\nScope: {scope}\nNo suggestions");
    super::scoped_outcome("link-suggest", &scope, payload, text)
}

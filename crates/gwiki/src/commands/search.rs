use serde_json::json;

use crate::CommandOutcome;
use crate::ScopeIdentity;

pub fn run(query: String, scope: ScopeIdentity, limit: usize) -> CommandOutcome {
    let payload = json!({
        "command": "search",
        "scope": scope,
        "query": query,
        "limit": limit,
        "results": [],
        "degradations": [],
    });
    let text = format!("Search results for \"{query}\"\nScope: {scope}\nNo results");
    super::scoped_outcome("search", &scope, payload, text)
}

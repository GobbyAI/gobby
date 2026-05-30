use serde_json::json;
use std::path::Path;

use crate::CommandOutcome;
use crate::ScopeIdentity;
use crate::vault::VaultPaths;

pub(crate) fn run(
    scope: ScopeIdentity,
    root: &Path,
    required_paths: &VaultPaths,
) -> CommandOutcome {
    let payload = json!({
        "command": "init",
        "scope": scope,
        "status": "ready",
        "root": root,
        "created": {
            "directories": required_paths.directories,
            "files": required_paths.files,
        },
    });
    let text = format!("Initialized wiki\nScope: {scope}\nRoot: {}", root.display());
    super::scoped_outcome("init", &scope, payload, text)
}

use std::path::Path;

use serde_json::json;

use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::vault::VaultPaths;
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError, registry, vault};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;

    vault::initialize(&scope)?;
    registry::register_scope(scope.registry_path(), &scope)?;

    let output_scope = resolved_scope_identity(&scope);
    let required_paths = vault::required_paths();
    Ok(render(output_scope, scope.root(), &required_paths))
}

fn render(scope: ScopeIdentity, root: &Path, required_paths: &VaultPaths) -> CommandOutcome {
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
    let text = format!(
        "Initialized wiki
Scope: {scope}
Root: {}",
        root.display()
    );
    super::scoped_outcome("init", &scope, payload, text)
}

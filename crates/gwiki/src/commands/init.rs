use std::path::Path;

use serde_json::json;

use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::vault::CreatedVaultPaths;
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError, registry, vault};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;

    let created_paths = vault::initialize(&scope)?;
    if let Err(error) = registry::register_scope(scope.registry_path(), &scope) {
        let _ = vault::cleanup_created(scope.root(), &created_paths);
        return Err(error);
    }

    let output_scope = resolved_scope_identity(&scope);
    Ok(render(output_scope, scope.root(), &created_paths))
}

fn render(scope: ScopeIdentity, root: &Path, created_paths: &CreatedVaultPaths) -> CommandOutcome {
    let payload = json!({
        "command": "init",
        "scope": scope,
        "status": "ready",
        "root": root.display().to_string(),
        "created": {
            "directories": created_paths.directories,
            "files": created_paths.files,
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

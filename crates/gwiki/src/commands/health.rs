use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{CommandOutcome, ScopeSelection, WikiError, health, vault};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    // health persists meta/health/ under the vault root, so claim the vault
    // first — an unclaimed write poisons the dir as a non-vault collision.
    vault::initialize(&scope)?;
    let output_scope = resolved_scope_identity(&scope);
    let report = health::run(scope.root(), output_scope.clone())?;
    let payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: "serialize health report",
        path: None,
        source: error,
    })?;
    Ok(super::scoped_outcome(
        "health",
        &output_scope,
        payload,
        health::render_text(&report),
    ))
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::execute;
    use crate::ScopeSelection;

    /// Regression (#17821): health writes `meta/health/` into the resolved
    /// vault dir, so it must claim the vault. An unclaimed write leaves a
    /// non-vault directory that the next resolution treats as a collision,
    /// burning one `gobby-wiki[-NNN]` fallback per call.
    #[test]
    fn health_claims_the_vault_so_repeat_runs_resolve_the_same_root() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let project = tmp.path().join("project");
        fs::create_dir_all(project.join(".gobby")).expect("create .gobby");
        fs::write(
            project.join(".gobby").join("gcode.json"),
            "{\n  \"id\": \"project-123\"\n}\n",
        )
        .expect("write gcode json");

        execute(ScopeSelection::project(&project)).expect("first health run");
        execute(ScopeSelection::project(&project)).expect("second health run");

        let canonical = project.canonicalize().expect("canonicalize project root");
        assert!(
            gobby_core::vault::is_vault(&canonical.join("wiki")),
            "health must claim the vault it writes into"
        );
        assert!(
            !canonical.join("gobby-wiki").exists(),
            "repeat runs must not burn fallback vault dirs"
        );
    }
}

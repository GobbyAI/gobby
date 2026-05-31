use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{CommandOutcome, ScopeSelection, WikiError, lint};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let report = lint::run(scope.root(), output_scope.clone())?;
    let payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: "serialize lint report",
        path: None,
        source: error,
    })?;
    Ok(super::scoped_outcome(
        "lint",
        &output_scope,
        payload,
        lint::render_text(&report),
    ))
}

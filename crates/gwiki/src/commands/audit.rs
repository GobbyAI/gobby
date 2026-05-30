use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{CommandOutcome, ScopeSelection, WikiError, audit};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let report = audit::run_with_options(
        scope.root(),
        output_scope.clone(),
        audit::AuditOptions::from_env(),
    )?;
    let payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: "serialize audit report",
        path: None,
        source: error.to_string(),
    })?;
    Ok(super::scoped_outcome(
        "audit",
        &output_scope,
        payload,
        audit::render_text(&report),
    ))
}

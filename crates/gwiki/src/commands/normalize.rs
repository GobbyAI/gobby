use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{CommandOutcome, ScopeSelection, WikiError, normalize};

pub(crate) fn execute(selection: ScopeSelection, check: bool) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let report = normalize::run(scope.root(), output_scope.clone(), check)?;
    let payload = serde_json::to_value(&report).map_err(|source| WikiError::Json {
        action: "serialize normalize report",
        path: None,
        source,
    })?;
    let mut outcome = super::scoped_outcome(
        "normalize",
        &output_scope,
        payload,
        normalize::render_text(&report),
    );
    // `--check` is a gate: a non-zero exit when docs still need normalization
    // lets CI fail on un-normalized markdown. Write mode always succeeds.
    outcome.exit_code = normalize::check_exit_code(&report);
    Ok(outcome)
}

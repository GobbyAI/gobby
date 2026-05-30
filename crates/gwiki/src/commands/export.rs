use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{CommandOutcome, ScopeSelection, WikiError, exports};

pub(crate) fn execute(
    selection: ScopeSelection,
    command: exports::ExportCommand,
) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let artifacts = exports::run(scope.root(), command)?;
    let output = exports::ExportOutput::new(output_scope.clone(), artifacts);
    let payload = serde_json::to_value(&output).map_err(|error| WikiError::Json {
        action: "serialize export output",
        path: None,
        source: error.to_string(),
    })?;
    let paths = output
        .artifacts
        .iter()
        .map(|artifact| artifact.path.display().to_string())
        .collect::<Vec<_>>()
        .join(", ");
    let text = format!(
        "Exported wiki artifacts
Scope: {output_scope}
Artifacts: {paths}"
    );
    Ok(super::scoped_outcome(
        "export",
        &output_scope,
        payload,
        text,
    ))
}

use crate::{CommandOutcome, ScopeSelection, WikiError, audit};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    super::run_analysis_command(
        "audit",
        selection,
        "serialize audit report",
        |root, output_scope| {
            audit::run_with_options(root, output_scope, audit::AuditOptions::from_env())
        },
        audit::render_text,
    )
}

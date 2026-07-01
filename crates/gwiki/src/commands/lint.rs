use crate::{CommandOutcome, ScopeSelection, WikiError, lint};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    super::run_analysis_command(
        "lint",
        selection,
        "serialize lint report",
        lint::run,
        lint::render_text,
    )
}

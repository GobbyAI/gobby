use crate::{CommandOutcome, ScopeSelection, WikiError, librarian};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    super::run_analysis_command(
        "librarian",
        selection,
        "serialize librarian proposals report",
        |root, scope| librarian::run(root, scope, librarian::Options::default()),
        librarian::render_text,
    )
}

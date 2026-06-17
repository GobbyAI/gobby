use crate::{CommandOutcome, ScopeSelection, WikiError, normalize};

pub(crate) fn execute(selection: ScopeSelection, check: bool) -> Result<CommandOutcome, WikiError> {
    super::run_analysis_command(
        "normalize",
        selection,
        "serialize normalize report",
        move |root, scope| normalize::run(root, scope, check),
        normalize::render_text,
    )
}

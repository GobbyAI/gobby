use crate::{Command, CommandOutcome, RunOptions, WikiError, commands};

/// Execute a parsed `gwiki` command through the public library boundary.
///
/// This passthrough is intentionally thin so embedders exercise the same
/// command dispatch path as the CLI binary.
pub fn run(command: Command, options: RunOptions) -> Result<CommandOutcome, WikiError> {
    commands::run(command, options)
}

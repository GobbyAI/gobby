use crate::{Command, CommandOutcome, WikiError, commands};

/// Execute a parsed `gwiki` command through the public library boundary.
///
/// This passthrough is intentionally thin so embedders exercise the same
/// command dispatch path as the CLI binary.
pub fn run(command: Command) -> Result<CommandOutcome, WikiError> {
    commands::run(command)
}

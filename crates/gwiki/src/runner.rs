use crate::{Command, CommandOutcome, ScopeSelection, WikiError, commands, session, support};

/// Execute a parsed `gwiki` command through the public library boundary.
///
/// This passthrough is intentionally thin so embedders exercise the same
/// command dispatch path as the CLI binary.
pub fn run(command: Command) -> Result<CommandOutcome, WikiError> {
    commands::run(command)
}

/// Resolve the research scope using the same project/topic rules as CLI commands.
pub fn resolve_research_scope(
    selection: &ScopeSelection,
) -> Result<session::ResearchScope, WikiError> {
    let scope = support::scope::resolve_command_scope(selection)?;
    Ok(session::ResearchScope::from(&scope))
}

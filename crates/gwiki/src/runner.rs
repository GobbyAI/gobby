use crate::{Command, CommandOutcome, ScopeSelection, WikiError, commands, session, support};

pub fn run(command: Command) -> Result<CommandOutcome, WikiError> {
    commands::run(command)
}

pub fn resolve_research_scope(
    selection: &ScopeSelection,
) -> Result<session::ResearchScope, WikiError> {
    let scope = support::scope::resolve_command_scope(selection)?;
    Ok(session::ResearchScope::from(&scope))
}

pub(crate) mod audit;
pub(crate) mod backlinks;
pub(crate) mod collect;
pub(crate) mod compile;
pub(crate) mod export;
pub(crate) mod health;
pub(crate) mod index;
pub(crate) mod init;
pub(crate) mod lint;
pub(crate) mod read;
pub(crate) mod research;
pub(crate) mod search;
pub(crate) mod setup;
pub(crate) mod status;

use crate::{Command, CommandOutcome, CommandResult, ScopeIdentity, WikiError};

pub(crate) fn run(command: Command) -> Result<CommandOutcome, WikiError> {
    match command {
        Command::Init { scope } => init::execute(scope),
        Command::Setup { scope, options } => setup::execute(scope, options),
        Command::Index { scope } => index::execute(scope),
        Command::Collect { scope } => collect::execute(scope),
        Command::IngestFile {
            path,
            scope,
            options,
        } => index::execute_ingest_file(path, scope, options),
        Command::Search {
            query,
            scope,
            limit,
            include_semantic,
        } => search::execute(query, scope, limit, include_semantic),
        Command::Read { target, scope } => read::execute(target, scope),
        Command::Backlinks { page, scope } => backlinks::execute(page, scope),
        Command::LinkSuggest { scope, limit } => backlinks::execute_link_suggest(scope, limit),
        Command::Research(options) => research::execute(options),
        Command::Compile {
            topic,
            outline,
            target_kind,
            target_page,
            write_intent,
            scope,
        } => compile::execute(
            topic,
            outline,
            target_kind,
            target_page,
            write_intent,
            scope,
        ),
        Command::Export { scope, command } => export::execute(scope, command),
        Command::Audit { scope } => audit::execute(scope),
        Command::Lint { scope } => lint::execute(scope),
        Command::Health { scope } => health::execute(scope),
        Command::Status { scope } => Ok(status::execute(scope)),
    }
}

pub(crate) fn scoped_outcome(
    command: &'static str,
    scope: &ScopeIdentity,
    payload: serde_json::Value,
    text: String,
) -> CommandOutcome {
    CommandOutcome {
        status_messages: vec![format!("{command} resolved scope {scope}")],
        result: CommandResult { payload, text },
    }
}

pub(crate) mod ask;
pub(crate) mod audit;
pub(crate) mod backlinks;
pub(crate) mod benchmark;
pub(crate) mod citation_quality;
pub(crate) mod collect;
pub(crate) mod compile;
pub(crate) mod export;
pub(crate) mod graph;
pub(crate) mod graph_context;
pub(crate) mod health;
pub(crate) mod index;
pub(crate) mod init;
pub(crate) mod lint;
pub(crate) mod read;
pub(crate) mod refresh;
pub(crate) mod research;
pub(crate) mod search;
pub(crate) mod setup;
pub(crate) mod sources;
pub(crate) mod status;
pub(crate) mod trust;

use std::path::Path;

use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{Command, CommandOutcome, CommandResult, ScopeIdentity, ScopeSelection, WikiError};

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
        Command::IngestUrl { urls, scope } => index::execute_ingest_url(urls, scope),
        Command::Refresh {
            scope,
            source_ids,
            dry_run,
        } => refresh::execute(scope, source_ids, dry_run),
        Command::Sources { scope } => sources::execute(scope),
        Command::RemoveSource {
            id,
            scope,
            dry_run,
            keep_asset,
        } => sources::execute_remove(id, scope, dry_run, keep_asset),
        Command::Search {
            query,
            scope,
            limit,
            include_semantic,
        } => search::execute(query, scope, limit, include_semantic),
        Command::Ask {
            query,
            scope,
            llm,
            ai,
            require_ai,
        } => ask::execute(query, scope, llm, ai, require_ai),
        Command::Read { target, scope } => read::execute(target, scope),
        Command::Backlinks { page, scope } => backlinks::execute(page, scope),
        Command::LinkSuggest { scope, limit } => backlinks::execute_link_suggest(scope, limit),
        Command::Benchmark { scope } => benchmark::execute(scope),
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
        Command::Graph { scope } => graph::execute(scope),
        Command::GraphContext { scope } => graph_context::execute(scope),
        Command::Audit { scope } => audit::execute(scope),
        Command::Lint { scope } => lint::execute(scope),
        Command::Health { scope } => health::execute(scope),
        Command::Status { scope } => status::execute(scope),
        Command::Trust { scope } => trust::execute(scope),
        Command::CitationQuality { scope } => citation_quality::execute(scope),
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
        exit_code: 0,
    }
}

pub(crate) fn run_analysis_command<T>(
    command: &'static str,
    selection: ScopeSelection,
    serialize_action: &'static str,
    run: impl FnOnce(&Path, ScopeIdentity) -> Result<T, WikiError>,
    render: impl FnOnce(&T) -> String,
) -> Result<CommandOutcome, WikiError>
where
    T: serde::Serialize,
{
    let scope = resolve_command_scope(&selection)?;
    let output_scope = resolved_scope_identity(&scope);
    let report = run(scope.root(), output_scope.clone())?;
    let payload = serde_json::to_value(&report).map_err(|error| WikiError::Json {
        action: serialize_action,
        path: None,
        source: error,
    })?;
    Ok(scoped_outcome(
        command,
        &output_scope,
        payload,
        render(&report),
    ))
}

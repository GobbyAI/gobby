pub(crate) mod ask;
pub(crate) mod audit;
pub(crate) mod backlinks;
pub(crate) mod benchmark;
pub(crate) mod citation_quality;
pub mod code;
pub(crate) mod collect;
pub(crate) mod compile;
pub(crate) mod export;
pub(crate) mod generation_routes;
pub(crate) mod graph;
pub(crate) mod graph_context;
pub(crate) mod health;
pub(crate) mod index;
pub(crate) mod init;
pub(crate) mod librarian;
pub(crate) mod lint;
pub(crate) mod normalize;
pub(crate) mod page;
pub(crate) mod pages;
pub(crate) mod paths;
mod project_admission;
pub(crate) mod prune;
pub(crate) mod purge;
pub(crate) mod read;
pub(crate) mod recap;
pub(crate) mod refresh;
pub(crate) mod review_report;
pub(crate) mod search;
pub(crate) mod session_sync;
pub(crate) mod sources;
pub(crate) mod status;
pub(crate) mod trust;
pub(crate) mod upkeep;
pub(crate) mod vault_tools;

use std::path::Path;

use crate::project_lock::run_with_project_lock;
use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{
    Command, CommandOutcome, CommandResult, RunOptions, ScopeIdentity, ScopeSelection, WikiError,
};

pub(crate) fn run(command: Command, run_options: RunOptions) -> Result<CommandOutcome, WikiError> {
    if let Some(root) = command_project_root(&command) {
        crate::support::env::set_active_project_root(Some(root));
    }
    let project_lock = project_admission::acquire_command_lock(&command)?;
    run_with_project_lock(project_lock, || dispatch(command, run_options))
}

fn command_project_root(command: &Command) -> Option<std::path::PathBuf> {
    match command {
        Command::Init { scope }
        | Command::Index { scope, .. }
        | Command::Collect { scope }
        | Command::IngestFile { scope, .. }
        | Command::IngestUrl { scope, .. }
        | Command::SyncSessions { scope, .. }
        | Command::Refresh { scope, .. }
        | Command::Sources { scope }
        | Command::RemoveSource { scope, .. }
        | Command::Search { scope, .. }
        | Command::Ask { scope, .. }
        | Command::Read { scope, .. }
        | Command::Pages { scope, .. }
        | Command::PageWrite { scope, .. }
        | Command::PageDelete { scope, .. }
        | Command::Backlinks { scope, .. }
        | Command::LinkSuggest { scope, .. }
        | Command::Benchmark { scope, .. }
        | Command::Compile { scope, .. }
        | Command::Export { scope, .. }
        | Command::Graph { scope, .. }
        | Command::GraphContext { scope }
        | Command::ReviewReport { scope, .. }
        | Command::Audit { scope }
        | Command::Lint { scope }
        | Command::Normalize { scope, .. }
        | Command::Health { scope }
        | Command::Librarian { scope, .. }
        | Command::Upkeep { scope, .. }
        | Command::Recap { scope, .. }
        | Command::Status { scope }
        | Command::Trust { scope }
        | Command::CitationQuality { scope } => scope.project_root().map(Path::to_path_buf),
        Command::Purge { target, .. } => match target {
            crate::PurgeTarget::Selection(scope) => scope.project_root().map(Path::to_path_buf),
            crate::PurgeTarget::ProjectId(_) => None,
        },
        Command::Code(_) | Command::Prune { .. } => None,
    }
}

fn dispatch(command: Command, run_options: RunOptions) -> Result<CommandOutcome, WikiError> {
    match command {
        Command::Init { scope } => init::execute(scope),
        Command::Index { scope, force } => index::execute(scope, run_options, force),
        Command::Collect { scope } => collect::execute(scope),
        Command::Code(options) => code::run_command(options).map_err(WikiError::from),
        Command::IngestFile {
            path,
            scope,
            options,
        } => index::execute_ingest_file(path, scope, options, run_options),
        Command::IngestUrl {
            urls,
            scope,
            max_age_hours,
        } => index::execute_ingest_url(urls, scope, max_age_hours, run_options),
        Command::SyncSessions { scope, options } => {
            session_sync::execute(scope, options, run_options)
        }
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
        Command::Purge { target, yes } => purge::execute(target, yes),
        Command::Prune { force } => prune::execute(force),
        Command::Search {
            query,
            scope,
            limit,
            include_semantic,
            token_budget,
            include_candidates,
        } => search::execute(
            query,
            scope,
            limit,
            include_semantic,
            token_budget,
            include_candidates,
        ),
        Command::Ask {
            query,
            scope,
            llm,
            deep,
            ai,
            token_budget,
            include_candidates,
        } => ask::execute(
            query,
            scope,
            llm,
            deep,
            ai,
            token_budget,
            include_candidates,
        ),
        Command::Read { target, scope } => read::execute(target, scope),
        Command::Pages { scope, prefix } => pages::execute(scope, prefix),
        Command::PageWrite {
            scope,
            path,
            mode,
            expected_hash,
        } => page::execute_write(scope, path, mode, expected_hash),
        Command::PageDelete { scope, path } => page::execute_delete(scope, path),
        Command::Backlinks { page, scope } => backlinks::execute(page, scope),
        Command::LinkSuggest { scope, limit } => backlinks::execute_link_suggest(scope, limit),
        Command::Benchmark { scope, options } => benchmark::execute(scope, options),
        Command::Compile {
            topic,
            outline,
            source,
            target_kind,
            target_page,
            write_intent,
            ai,
            scope,
        } => compile::execute(
            topic,
            outline,
            source,
            target_kind,
            target_page,
            write_intent,
            ai,
            scope,
        ),
        Command::Export { scope, command } => export::execute(scope, command),
        Command::Graph { scope, options } => graph::execute(scope, options),
        Command::GraphContext { scope } => graph_context::execute(scope),
        Command::ReviewReport { scope, options } => review_report::execute(scope, options),
        Command::Audit { scope } => audit::execute(scope),
        Command::Lint { scope } => lint::execute(scope),
        Command::Normalize { scope, check } => normalize::execute(scope, check),
        Command::Health { scope } => health::execute(scope),
        Command::Librarian { scope, ai } => librarian::execute(scope, ai),
        Command::Upkeep { scope, options, ai } => upkeep::execute(scope, options, ai),
        Command::Recap { scope, options, ai } => recap::execute(scope, options, ai),
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

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
pub(crate) mod lanes;
pub(crate) mod librarian;
pub(crate) mod lint;
pub(crate) mod normalize;
pub(crate) mod page;
pub(crate) mod pages;
pub(crate) mod paths;
pub(crate) mod purge;
pub(crate) mod read;
pub(crate) mod recap;
pub(crate) mod refresh;
pub(crate) mod review_report;
pub(crate) mod search;
pub(crate) mod session_sync;
pub(crate) mod setup;
pub(crate) mod sources;
pub(crate) mod status;
pub(crate) mod trust;
pub(crate) mod upkeep;
pub(crate) mod vault_tools;

use std::path::Path;

use crate::project_lock::{
    ProjectLockGuard, acquire_purge_lock, acquire_writer_lock, run_with_project_lock,
};
use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{
    Command, CommandOutcome, CommandResult, RunOptions, ScopeIdentity, ScopeSelection, WikiError,
};

pub(crate) fn run(command: Command, run_options: RunOptions) -> Result<CommandOutcome, WikiError> {
    let project_lock = acquire_command_lock(&command)?;
    run_with_project_lock(project_lock, || dispatch(command, run_options))
}

fn dispatch(command: Command, run_options: RunOptions) -> Result<CommandOutcome, WikiError> {
    match command {
        Command::Init { scope } => init::execute(scope),
        Command::Setup { scope, options } => setup::execute(scope, options),
        Command::Index { scope, force } => index::execute(scope, run_options, force),
        Command::Collect { scope } => collect::execute(scope),
        Command::IngestFile {
            path,
            scope,
            options,
        } => index::execute_ingest_file(path, scope, options, run_options),
        Command::IngestUrl { urls, scope } => index::execute_ingest_url(urls, scope, run_options),
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
        Command::Purge { scope, yes } => purge::execute(scope, yes),
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
            ai,
            require_ai,
            token_budget,
            include_candidates,
        } => ask::execute(
            query,
            scope,
            llm,
            ai,
            require_ai,
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

#[derive(Debug)]
enum CommandClassification<'a> {
    PersistentWriter {
        scope: &'a ScopeSelection,
        command: &'static str,
    },
    ExplicitPurge {
        scope: &'a ScopeSelection,
    },
    TopicOnly,
    ReadOnly,
}

fn classify_command(command: &Command) -> CommandClassification<'_> {
    match command {
        Command::Index { scope, .. } => CommandClassification::PersistentWriter {
            scope,
            command: "gwiki index",
        },
        Command::Collect { scope } => CommandClassification::PersistentWriter {
            scope,
            command: "gwiki collect",
        },
        Command::IngestFile { scope, .. } => CommandClassification::PersistentWriter {
            scope,
            command: "gwiki ingest-file",
        },
        Command::IngestUrl { scope, .. } => CommandClassification::PersistentWriter {
            scope,
            command: "gwiki ingest-url",
        },
        Command::SyncSessions {
            scope: ScopeSelection::Detect,
            ..
        } => CommandClassification::TopicOnly,
        Command::SyncSessions { scope, .. } => CommandClassification::PersistentWriter {
            scope,
            command: "gwiki sync-sessions",
        },
        Command::Refresh {
            scope,
            dry_run: false,
            ..
        } => CommandClassification::PersistentWriter {
            scope,
            command: "gwiki refresh",
        },
        Command::RemoveSource {
            scope,
            dry_run: false,
            ..
        } => CommandClassification::PersistentWriter {
            scope,
            command: "gwiki remove-source",
        },
        Command::Purge { scope, .. } => CommandClassification::ExplicitPurge { scope },
        Command::Init { .. }
        | Command::Setup { .. }
        | Command::Refresh { dry_run: true, .. }
        | Command::Sources { .. }
        | Command::RemoveSource { dry_run: true, .. }
        | Command::Search { .. }
        | Command::Ask { .. }
        | Command::Read { .. }
        | Command::Pages { .. }
        | Command::PageWrite { .. }
        | Command::PageDelete { .. }
        | Command::Backlinks { .. }
        | Command::LinkSuggest { .. }
        | Command::Benchmark { .. }
        | Command::Compile { .. }
        | Command::Export { .. }
        | Command::Graph { .. }
        | Command::GraphContext { .. }
        | Command::ReviewReport { .. }
        | Command::Audit { .. }
        | Command::Lint { .. }
        | Command::Normalize { .. }
        | Command::Health { .. }
        | Command::Librarian { .. }
        | Command::Upkeep { .. }
        | Command::Recap { .. }
        | Command::Status { .. }
        | Command::Trust { .. }
        | Command::CitationQuality { .. } => CommandClassification::ReadOnly,
    }
}

fn acquire_command_lock(command: &Command) -> Result<Option<ProjectLockGuard>, WikiError> {
    match classify_command(command) {
        CommandClassification::PersistentWriter { scope, command } => {
            let Some(project_id) = project_id_for_admission(scope)? else {
                return Ok(None);
            };
            acquire_writer_lock(&project_id, command).map(Some)
        }
        CommandClassification::ExplicitPurge { scope } => {
            let Some(project_id) = project_id_for_admission(scope)? else {
                return Ok(None);
            };
            acquire_purge_lock(&project_id).map(Some)
        }
        CommandClassification::TopicOnly | CommandClassification::ReadOnly => Ok(None),
    }
}

fn project_id_for_admission(selection: &ScopeSelection) -> Result<Option<String>, WikiError> {
    if selection.topic_name().is_some() {
        return Ok(None);
    }
    let scope = resolve_command_scope(selection)?;
    Ok(scope.project_id().map(str::to_owned))
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

#[cfg(test)]
mod project_lock_tests {
    use std::path::PathBuf;

    use super::*;
    use crate::{IngestFileOptions, SyncSessionsOptions};

    #[test]
    fn dispatch_classification_pins_every_persistent_writer_arm() {
        let scope = || ScopeSelection::topic("classification-fixture");
        let commands = vec![
            Command::Index {
                scope: scope(),
                force: false,
            },
            Command::Collect { scope: scope() },
            Command::IngestFile {
                path: PathBuf::from("source.md"),
                scope: scope(),
                options: IngestFileOptions {
                    no_ai: true,
                    translate: false,
                    target_lang: None,
                    video_frame_interval_seconds: None,
                    transcription_routing: None,
                    vision_routing: None,
                    text_routing: None,
                },
            },
            Command::IngestUrl {
                urls: vec!["https://example.com".to_string()],
                scope: scope(),
            },
            Command::SyncSessions {
                scope: scope(),
                options: SyncSessionsOptions::default(),
            },
            Command::Refresh {
                scope: scope(),
                source_ids: Vec::new(),
                dry_run: false,
            },
            Command::RemoveSource {
                id: "source-id".to_string(),
                scope: scope(),
                dry_run: false,
                keep_asset: false,
            },
        ];

        for command in commands {
            assert!(matches!(
                classify_command(&command),
                CommandClassification::PersistentWriter { .. }
            ));
        }
    }

    #[test]
    fn dry_run_variants_are_classified_as_read_only() {
        for command in [
            Command::Refresh {
                scope: ScopeSelection::topic("classification-fixture"),
                source_ids: Vec::new(),
                dry_run: true,
            },
            Command::RemoveSource {
                id: "source-id".to_string(),
                scope: ScopeSelection::topic("classification-fixture"),
                dry_run: true,
                keep_asset: false,
            },
        ] {
            assert!(matches!(
                classify_command(&command),
                CommandClassification::ReadOnly
            ));
        }
    }

    #[test]
    fn purge_has_its_own_serialized_cleanup_classification() {
        let command = Command::Purge {
            scope: ScopeSelection::topic("classification-fixture"),
            yes: true,
        };

        assert!(matches!(
            classify_command(&command),
            CommandClassification::ExplicitPurge { .. }
        ));
    }

    #[test]
    fn topic_scopes_skip_project_lock_admission() {
        let selection = ScopeSelection::topic("classification-fixture");

        assert_eq!(
            project_id_for_admission(&selection).expect("topic scope"),
            None
        );
    }
}

use crate::project_lock::{ProjectLockGuard, acquire_purge_lock, acquire_writer_lock};
use crate::support::scope::resolve_command_scope;
use crate::{Command, PurgeTarget, ScopeSelection, WikiError};

#[derive(Debug)]
enum CommandClassification<'a> {
    PersistentWriter {
        scope: &'a ScopeSelection,
        command: &'static str,
    },
    ExplicitPurge {
        target: &'a PurgeTarget,
    },
    Code,
    TopicOnly,
    ReadOnly,
}

fn classify_command(command: &Command) -> CommandClassification<'_> {
    match command {
        Command::Index { scope, .. } => writer(scope, "gwiki index"),
        Command::Collect { scope } => writer(scope, "gwiki collect"),
        Command::IngestFile { scope, .. } => writer(scope, "gwiki ingest-file"),
        Command::IngestUrl { scope, .. } => writer(scope, "gwiki ingest-url"),
        Command::SyncSessions {
            scope: ScopeSelection::Detect,
            ..
        } => CommandClassification::TopicOnly,
        Command::SyncSessions { scope, .. } => writer(scope, "gwiki sync-sessions"),
        Command::Refresh {
            scope,
            dry_run: false,
            ..
        } => writer(scope, "gwiki refresh"),
        Command::RemoveSource {
            scope,
            dry_run: false,
            ..
        } => writer(scope, "gwiki remove-source"),
        Command::Purge { target, .. } => CommandClassification::ExplicitPurge { target },
        Command::Init { .. }
        | Command::Prune { .. }
        | Command::Refresh { dry_run: true, .. }
        | Command::Sources { .. }
        | Command::RemoveSource { dry_run: true, .. }
        | Command::Search { .. }
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
        Command::Code(_) => CommandClassification::Code,
    }
}

fn writer<'a>(scope: &'a ScopeSelection, command: &'static str) -> CommandClassification<'a> {
    CommandClassification::PersistentWriter { scope, command }
}

pub(super) fn acquire_command_lock(
    command: &Command,
) -> Result<Option<ProjectLockGuard>, WikiError> {
    match classify_command(command) {
        CommandClassification::PersistentWriter { scope, command } => {
            let Some(project_id) = project_id_for_admission(scope)? else {
                return Ok(None);
            };
            acquire_writer_lock(&project_id, command).map(Some)
        }
        CommandClassification::ExplicitPurge { target } => {
            let Some(project_id) = purge_project_id_for_admission(target)? else {
                return Ok(None);
            };
            acquire_purge_lock(&project_id).map(Some)
        }
        CommandClassification::Code
        | CommandClassification::TopicOnly
        | CommandClassification::ReadOnly => Ok(None),
    }
}

fn purge_project_id_for_admission(target: &PurgeTarget) -> Result<Option<String>, WikiError> {
    match target {
        PurgeTarget::ProjectId(project_id) => Ok(Some(project_id.clone())),
        PurgeTarget::Selection(selection) => project_id_for_admission(selection),
    }
}

fn project_id_for_admission(selection: &ScopeSelection) -> Result<Option<String>, WikiError> {
    if selection.topic_name().is_some() {
        return Ok(None);
    }
    let scope = resolve_command_scope(selection)?;
    Ok(scope.project_id().map(str::to_owned))
}

#[cfg(test)]
#[path = "project_admission/tests.rs"]
mod tests;

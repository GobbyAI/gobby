use std::path::{Path, PathBuf};

use serde_json::json;

use crate::commands::index::{
    StderrWikiProgress, connect_postgres_index, indexed_counts_for_postgres,
    postgres_store_for_search, sync_falkor_graph, sync_qdrant_vectors,
};
use crate::ingest::{self, session_archive};
use crate::progress::ProgressOptions;
use crate::search::SearchScope;
use crate::support::counts::IndexCounts;
use crate::support::env::database_url_for;
use crate::support::scope::{
    resolve_command_scope, resolved_scope_identity, search_scope_for_resolved,
};
use crate::support::time::collect_timestamp;
use crate::{
    CommandOutcome, RunOptions, ScopeIdentity, ScopeSelection, SyncSessionsOptions, WikiError,
    vault,
};

const COMMAND: &str = "gwiki sync-sessions";

struct SessionSyncPhaseContext<'a, 'progress> {
    conn: &'a mut postgres::Client,
    search_scope: &'a SearchScope,
    progress: &'a mut ProgressOptions<'progress>,
}

/// Machine-global topic scope that daemon-synthesized session wikis are ingested
/// into. The daemon writes session pages to one flat global directory
/// (`~/.gobby/session_wiki/`), so they belong in a single machine-global scope
/// rather than any one project's vault — projecting them per-project
/// cross-contaminates every project that runs `gwiki sync-sessions`. See
/// gobby-cli #940 / session-wiki Model B.
pub(crate) const SESSIONS_TOPIC: &str = "sessions";

/// Default a `sync-sessions` scope selection to the machine-global `sessions`
/// topic. A bare invocation (`Detect`) targets the reserved topic; an explicit
/// `--project`/`--topic` selection is preserved as an escape hatch.
fn default_sessions_scope(selection: ScopeSelection) -> ScopeSelection {
    match selection {
        ScopeSelection::Detect => ScopeSelection::topic(SESSIONS_TOPIC),
        other => other,
    }
}

pub(crate) fn execute(
    selection: ScopeSelection,
    options: SyncSessionsOptions,
    run_options: RunOptions,
) -> Result<CommandOutcome, WikiError> {
    execute_with_database_url(selection, options, run_options, || {
        database_url_for(COMMAND)
    })
}

fn execute_with_database_url(
    selection: ScopeSelection,
    options: SyncSessionsOptions,
    run_options: RunOptions,
    database_url: impl FnOnce() -> Result<Option<String>, WikiError>,
) -> Result<CommandOutcome, WikiError> {
    let selection = default_sessions_scope(selection);
    let scope = resolve_command_scope(&selection)?;
    let initialized = vault::initialize(&scope)?;
    if !initialized.directories.is_empty() || !initialized.files.is_empty() {
        log::debug!(
            "initialized gwiki vault paths before sync-sessions: directories={:?} files={:?}",
            initialized.directories,
            initialized.files
        );
    }

    let archive_dir = archive_dir_or_default(options.archive_dir)?;
    let wiki_dir = wiki_dir_or_default(options.wiki_dir)?;
    log::debug!(
        "resolved session wiki directory for sync-sessions: {}",
        wiki_dir.display()
    );
    let output_scope = resolved_scope_identity(&scope);
    let fetched_at = collect_timestamp()?;
    // `--summarize` takes precedence over `--raw`: it also processes raw archives,
    // just with an LLM summary instead of a skeleton.
    let raw_mode = if options.summarize {
        session_archive::RawArchiveMode::Summarize
    } else if options.raw {
        session_archive::RawArchiveMode::Skeleton
    } else {
        session_archive::RawArchiveMode::Skip
    };
    let mut stderr_progress = StderrWikiProgress::new(run_options.quiet);
    let mut progress = ProgressOptions::with_sink(&mut stderr_progress);
    if let Some(database_url) = database_url()? {
        let mut conn = connect_postgres_index(&database_url, COMMAND)?;
        let search_scope = search_scope_for_resolved(&scope);
        let mut phase_context = SessionSyncPhaseContext {
            conn: &mut conn,
            search_scope: &search_scope,
            progress: &mut progress,
        };
        let result = run_persistent_write_phases(
            &mut phase_context,
            |context| {
                let mut store = postgres_store_for_search(&mut *context.conn, context.search_scope);
                session_archive::sync_session_transcript_archives(
                    scope.root(),
                    &mut store,
                    session_archive::SessionArchiveSyncRequest {
                        archive_dir: &archive_dir,
                        wiki_dir: &wiki_dir,
                        limit: options.limit,
                        raw_mode,
                        enrich: options.enrich,
                        fetched_at: &fetched_at,
                    },
                    &mut *context.progress,
                )
            },
            |result| result.has_changes(),
            |context| {
                sync_qdrant_vectors(
                    &mut *context.conn,
                    context.search_scope,
                    COMMAND,
                    &mut *context.progress,
                )?;
                Ok(())
            },
            |context| {
                sync_falkor_graph(
                    &mut *context.conn,
                    context.search_scope,
                    COMMAND,
                    &mut *context.progress,
                )?;
                Ok(())
            },
        )?;
        let counts = indexed_counts_for_postgres(&mut conn, &search_scope, true)?;
        crate::log::append_sources_ingested(
            scope.root(),
            &output_scope,
            &fetched_at,
            result.accepted.iter().map(|accepted| &accepted.result),
        )?;
        return Ok(render_sync_sessions(output_scope, &result, counts));
    }

    Err(WikiError::from(
        gobby_core::grant::GrantError::DaemonRequired,
    ))
}

pub(super) fn run_persistent_write_phases<C, T>(
    context: &mut C,
    postgres_write: impl FnOnce(&mut C) -> Result<T, WikiError>,
    has_changes: impl FnOnce(&T) -> bool,
    qdrant_sync: impl FnOnce(&mut C) -> Result<(), WikiError>,
    falkor_sync: impl FnOnce(&mut C) -> Result<(), WikiError>,
) -> Result<T, WikiError> {
    let result = postgres_write(context)?;
    // Reconciled deletions change the index just like accepted ingests, so
    // gate Qdrant/Falkor sync on any change, not only newly accepted pages.
    if has_changes(&result) {
        qdrant_sync(context)?;
        falkor_sync(context)?;
    }
    Ok(result)
}

fn archive_dir_or_default(archive_dir: Option<PathBuf>) -> Result<PathBuf, WikiError> {
    match archive_dir {
        Some(path) => Ok(path),
        None => gobby_core::gobby_home()
            .map(|home| home.join("session_transcripts"))
            .map_err(|error| WikiError::Config {
                detail: format!(
                    "failed to resolve Gobby home for session transcript sync: {error}"
                ),
            }),
    }
}

fn wiki_dir_or_default(wiki_dir: Option<PathBuf>) -> Result<PathBuf, WikiError> {
    match wiki_dir {
        Some(path) => Ok(path),
        None => gobby_core::gobby_home()
            .map(|home| home.join("session_wiki"))
            .map_err(|error| WikiError::Config {
                detail: format!("failed to resolve Gobby home for session wiki sync: {error}"),
            }),
    }
}

fn render_sync_sessions(
    scope: ScopeIdentity,
    result: &session_archive::SessionArchiveBatchIngest,
    counts: IndexCounts,
) -> CommandOutcome {
    let accepted = result
        .accepted
        .iter()
        .map(|accepted| {
            json!({
                "archive_path": accepted.archive_path,
                "raw_path": accepted.result.raw_path,
                "source": {
                    "id": &accepted.result.record.id,
                    "kind": &accepted.result.record.kind,
                    "content_hash": &accepted.result.record.content_hash,
                    "location": &accepted.result.record.location,
                },
            })
        })
        .collect::<Vec<_>>();
    let skipped = result
        .skipped
        .iter()
        .map(|skipped| {
            json!({
                "archive_path": skipped.archive_path,
                "content_hash": skipped.content_hash,
                "reason": skipped.reason,
            })
        })
        .collect::<Vec<_>>();
    let failed = result
        .failed
        .iter()
        .map(|failure| {
            json!({
                "archive_path": failure.archive_path,
                "code": failure.code,
                "message": failure.message,
            })
        })
        .collect::<Vec<_>>();
    let reconciled = result
        .reconciled
        .iter()
        .map(|reconciled| {
            json!({
                "source_id": reconciled.source_id,
                "canonical_location": reconciled.canonical_location,
                "content_hash": reconciled.content_hash,
            })
        })
        .collect::<Vec<_>>();
    let payload = json!({
        "command": "sync-sessions",
        "scope": scope,
        "status": result.status(),
        "archive_dir": result.archive_dir,
        "scanned": result.scanned,
        "accepted": accepted,
        "skipped": skipped,
        "failed": failed,
        "reconciled": reconciled,
        "indexed": {
            "documents": counts.documents,
            "chunks": counts.chunks,
            "links": counts.links,
            "sources": counts.sources,
            "ingestions": counts.ingestions,
        },
    });
    let text = format!(
        "Synced session archives\nScope: {scope}\nArchive dir: {}\nStatus: {}\nScanned: {}\nIngested: {}\nSkipped: {}\nFailed: {}\nReconciled: {}\nDocuments: {}\nChunks: {}\nLinks: {}\nSources: {}\nIngestions: {}",
        ingest::path_to_string(Path::new(&result.archive_dir)),
        result.status(),
        result.scanned,
        result.accepted.len(),
        result.skipped.len(),
        result.failed.len(),
        result.reconciled.len(),
        counts.documents,
        counts.chunks,
        counts.links,
        counts.sources,
        counts.ingestions
    );
    let mut outcome = super::scoped_outcome("sync-sessions", &scope, payload, text);
    outcome.exit_code = result.exit_code();
    outcome
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::support::test_env::EnvGuard;

    #[test]
    fn default_sessions_scope_maps_detect_to_sessions_topic() {
        assert_eq!(
            default_sessions_scope(ScopeSelection::Detect),
            ScopeSelection::topic(SESSIONS_TOPIC)
        );
    }

    #[test]
    fn default_sessions_scope_preserves_explicit_selections() {
        let project = ScopeSelection::project("/tmp/example-project");
        assert_eq!(default_sessions_scope(project.clone()), project);

        let topic = ScopeSelection::topic("rust-async");
        assert_eq!(default_sessions_scope(topic.clone()), topic);
    }

    #[test]
    fn execute_defaults_detect_to_sessions_topic() {
        // Regression guard against the flip being applied somewhere that still
        // carries `Detect` (e.g. only at `command_from_cli`): drive the public
        // `execute` entry point and assert the resolved scope is the topic.
        let temp = tempfile::tempdir().expect("tempdir");
        let hub = temp.path().join("wiki-hub");
        let wiki_dir = temp.path().join("session_wiki");
        let archive_dir = temp.path().join("archive");
        std::fs::create_dir_all(&wiki_dir).expect("wiki dir");
        std::fs::create_dir_all(&archive_dir).expect("archive dir");

        // Injecting no database selects the in-memory store path.
        // GOBBY_WIKI_HUB keeps topic vault initialization inside the tempdir.
        let _env = EnvGuard::set("GOBBY_WIKI_HUB", hub.as_os_str());

        let options = SyncSessionsOptions {
            wiki_dir: Some(wiki_dir),
            archive_dir: Some(archive_dir),
            ..Default::default()
        };

        let outcome = execute_with_database_url(
            ScopeSelection::Detect,
            options,
            RunOptions { quiet: true },
            || Ok(None),
        )
        .expect("execute sync-sessions");
        assert_eq!(outcome.result.payload["scope"]["kind"], "topic");
        assert_eq!(outcome.result.payload["scope"]["id"], SESSIONS_TOPIC);
    }
}

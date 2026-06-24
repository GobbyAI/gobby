use std::path::{Path, PathBuf};

use serde_json::json;

use crate::commands::index::{
    connect_postgres_index, indexed_counts_for_postgres, postgres_store_for_search,
    sync_falkor_graph, sync_qdrant_vectors,
};
use crate::ingest::{self, session_archive};
use crate::support::counts::{IndexCounts, index_counts};
use crate::support::env::database_url_for;
use crate::support::scope::{
    resolve_command_scope, resolved_scope_identity, search_scope_for_resolved,
};
use crate::support::time::collect_timestamp;
use crate::{
    CommandOutcome, ScopeIdentity, ScopeSelection, SyncSessionsOptions, WikiError, store, vault,
};

const COMMAND: &str = "gwiki sync-sessions";

pub(crate) fn execute(
    selection: ScopeSelection,
    options: SyncSessionsOptions,
) -> Result<CommandOutcome, WikiError> {
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
    if let Some(database_url) = database_url_for(COMMAND)? {
        let mut conn = connect_postgres_index(&database_url, COMMAND)?;
        let search_scope = search_scope_for_resolved(&scope);
        let result = {
            let mut store = postgres_store_for_search(&mut conn, &search_scope);
            session_archive::sync_session_transcript_archives(
                scope.root(),
                &mut store,
                &archive_dir,
                options.limit,
                &fetched_at,
            )?
        };
        let counts = indexed_counts_for_postgres(&mut conn, &search_scope, true)?;
        if !result.accepted.is_empty() {
            sync_qdrant_vectors(&mut conn, &search_scope, COMMAND)?;
            sync_falkor_graph(&mut conn, &search_scope, COMMAND)?;
        }
        return Ok(render_sync_sessions(output_scope, &result, counts));
    }

    let mut store = store::MemoryWikiStore::default();
    let result = session_archive::sync_session_transcript_archives(
        scope.root(),
        &mut store,
        &archive_dir,
        options.limit,
        &fetched_at,
    )?;
    let counts = index_counts(&store);
    Ok(render_sync_sessions(output_scope, &result, counts))
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
    let payload = json!({
        "command": "sync-sessions",
        "scope": scope,
        "status": result.status(),
        "archive_dir": result.archive_dir,
        "scanned": result.scanned,
        "accepted": accepted,
        "skipped": skipped,
        "failed": failed,
        "indexed": {
            "documents": counts.documents,
            "chunks": counts.chunks,
            "links": counts.links,
            "sources": counts.sources,
            "ingestions": counts.ingestions,
        },
    });
    let text = format!(
        "Synced session archives\nScope: {scope}\nArchive dir: {}\nStatus: {}\nScanned: {}\nIngested: {}\nSkipped: {}\nFailed: {}\nDocuments: {}\nChunks: {}\nLinks: {}\nSources: {}\nIngestions: {}",
        ingest::path_to_string(Path::new(&result.archive_dir)),
        result.status(),
        result.scanned,
        result.accepted.len(),
        result.skipped.len(),
        result.failed.len(),
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

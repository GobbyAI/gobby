use std::path::{Path, PathBuf};

use serde_json::json;

use crate::error::index_error_to_wiki_error;
use crate::ingest::{self, IngestResult};
use crate::support::counts::{IndexCounts, index_counts, postgres_index_counts};
use crate::support::env::database_url_from_env;
use crate::support::scope::{
    resolve_command_scope, resolved_scope_identity, search_scope_for_resolved,
    store_scope_for_search,
};
use crate::support::time::collect_timestamp;
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError, indexer, store, vault};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    ensure_scope_root(&scope)?;
    let output_scope = resolved_scope_identity(&scope);
    if let Some(database_url) = database_url_from_env() {
        let mut conn = gobby_core::postgres::connect_readwrite(&database_url).map_err(|error| {
            WikiError::Config {
                detail: format!("failed to connect to PostgreSQL for gwiki index: {error}"),
            }
        })?;
        let search_scope = search_scope_for_resolved(&scope);
        let mut store =
            store::PostgresWikiStore::new(&mut conn, store_scope_for_search(&search_scope));
        indexer::index_vault(scope.root(), &mut store).map_err(index_error_to_wiki_error)?;
        let counts = postgres_index_counts(&mut conn, &search_scope)?;
        return Ok(render_index(output_scope, scope.root(), counts));
    }

    let mut store = store::MemoryWikiStore::default();
    indexer::index_vault(scope.root(), &mut store).map_err(index_error_to_wiki_error)?;
    let counts = index_counts(&store);
    Ok(render_index(output_scope, scope.root(), counts))
}

pub(crate) fn execute_ingest_file(
    path: PathBuf,
    selection: ScopeSelection,
) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    let _ = vault::initialize(&scope)?;
    let output_scope = resolved_scope_identity(&scope);
    let fetched_at = collect_timestamp().map_err(|error| WikiError::Config {
        detail: format!("failed to read system clock: {error}"),
    })?;
    if let Some(database_url) = database_url_from_env() {
        let mut conn = gobby_core::postgres::connect_readwrite(&database_url).map_err(|error| {
            WikiError::Config {
                detail: format!("failed to connect to PostgreSQL for gwiki ingest-file: {error}"),
            }
        })?;
        let search_scope = search_scope_for_resolved(&scope);
        let result = {
            let mut store =
                store::PostgresWikiStore::new(&mut conn, store_scope_for_search(&search_scope));
            ingest::file::ingest_path(scope.root(), &mut store, &path, &fetched_at)?
        };
        let counts = postgres_index_counts(&mut conn, &search_scope)?;
        return Ok(render_ingest_file(&path, output_scope, &result, counts));
    }

    let mut store = store::MemoryWikiStore::default();
    let result = ingest::file::ingest_path(scope.root(), &mut store, &path, &fetched_at)?;
    let counts = index_counts(&store);
    Ok(render_ingest_file(&path, output_scope, &result, counts))
}

fn render_index(scope: ScopeIdentity, root: &Path, counts: IndexCounts) -> CommandOutcome {
    let payload = json!({
        "command": "index",
        "scope": scope,
        "status": "indexed",
        "root": root,
        "indexed": {
            "documents": counts.documents,
            "chunks": counts.chunks,
            "links": counts.links,
            "sources": counts.sources,
            "ingestions": counts.ingestions,
        },
    });
    let text = format!(
        "Index complete
Scope: {scope}
Documents: {}
Chunks: {}
Links: {}
Sources: {}
Ingestions: {}",
        counts.documents, counts.chunks, counts.links, counts.sources, counts.ingestions
    );
    super::scoped_outcome("index", &scope, payload, text)
}

fn render_ingest_file(
    path: &Path,
    scope: ScopeIdentity,
    result: &IngestResult,
    counts: IndexCounts,
) -> CommandOutcome {
    let payload = json!({
        "command": "ingest-file",
        "scope": scope,
        "status": "ingested",
        "path": path,
        "raw_path": &result.raw_path,
        "asset_path": &result.asset_path,
        "source": {
            "id": &result.record.id,
            "kind": &result.record.kind,
            "content_hash": &result.record.content_hash,
            "location": &result.record.location,
        },
        "indexed": {
            "documents": counts.documents,
            "chunks": counts.chunks,
            "links": counts.links,
            "sources": counts.sources,
            "ingestions": counts.ingestions,
        },
    });
    let text = format!(
        "Ingested file
Scope: {scope}
Raw: {}
Asset: {}
Source: {} ({})
Content hash: {}
Documents: {}
Chunks: {}
Links: {}
Sources: {}
Ingestions: {}",
        ingest::path_to_string(&result.raw_path),
        result
            .asset_path
            .as_ref()
            .map(|path| ingest::path_to_string(path))
            .unwrap_or_else(|| "<none>".to_string()),
        result.record.location,
        result.record.kind,
        result.record.content_hash,
        counts.documents,
        counts.chunks,
        counts.links,
        counts.sources,
        counts.ingestions
    );
    super::scoped_outcome("ingest-file", &scope, payload, text)
}

fn ensure_scope_root(scope: &crate::scope::ResolvedScope) -> Result<(), WikiError> {
    if scope.root().is_dir() {
        return Ok(());
    }
    Err(WikiError::InvalidScope {
        detail: format!(
            "wiki scope root is missing or not a directory: {}",
            scope.root().display()
        ),
    })
}

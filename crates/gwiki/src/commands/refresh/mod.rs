use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use serde::Serialize;
use serde_json::json;

use crate::commands::index;
use crate::ingest;
use crate::ingest::url::{UrlIngestFailure, UrlSnapshot};
use crate::scope::ResolvedScope;
use crate::sources::{SourceKind, SourceManifest, SourceRecord, SourceReplay, SourceReplayOptions};
use crate::support::counts::IndexCounts;
use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::support::time::collect_timestamp;
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError};

mod candidate;
mod model;
mod render;
mod selection;
mod vault;

use candidate::*;
use model::*;
use render::*;
use selection::*;
use vault::*;

pub(crate) fn execute(
    selection: ScopeSelection,
    source_ids: Vec<String>,
    dry_run: bool,
) -> Result<CommandOutcome, WikiError> {
    execute_with_fetcher(selection, source_ids, dry_run, |record, fetched_at| {
        ingest::url::fetch_url_snapshot(refresh_url(record), fetched_at)
    })
}

fn execute_with_fetcher(
    selection: ScopeSelection,
    source_ids: Vec<String>,
    dry_run: bool,
    mut fetch: impl FnMut(&SourceRecord, &str) -> Result<UrlSnapshot, UrlIngestFailure>,
) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    execute_resolved_with_fetcher(scope, source_ids, dry_run, |record, fetched_at| {
        fetch(record, fetched_at)
    })
}

fn execute_resolved_with_fetcher(
    scope: ResolvedScope,
    source_ids: Vec<String>,
    dry_run: bool,
    mut fetch: impl FnMut(&SourceRecord, &str) -> Result<UrlSnapshot, UrlIngestFailure>,
) -> Result<CommandOutcome, WikiError> {
    ensure_scope_root(scope.root())?;
    let output_scope = resolved_scope_identity(&scope);
    let manifest = SourceManifest::read(scope.root())?;
    let explicit = !source_ids.is_empty();
    let Selection {
        planned,
        skipped,
        mut failed,
    } = select_sources(&manifest.entries, &source_ids);

    if dry_run {
        return Ok(render_refresh(RefreshRender {
            scope: output_scope,
            dry_run,
            planned,
            refreshed: Vec::new(),
            unchanged: Vec::new(),
            failed,
            skipped,
            index_status: IndexStatus::not_run(),
            degradations: Vec::new(),
            explicit,
        }));
    }

    let fetched_at = collect_timestamp()?;
    let mut refreshed = Vec::new();
    let mut unchanged = Vec::new();
    let mut degradations = Vec::new();

    for plan in &planned {
        let record = &plan.record;
        match replay_kind(record) {
            Ok(ReplayKind::Url) => {
                refresh_url_candidate(
                    scope.root(),
                    record,
                    &mut fetch,
                    &fetched_at,
                    &mut refreshed,
                    &mut unchanged,
                    &mut failed,
                )?;
            }
            Ok(ReplayKind::LocalFile) => {
                let mut sinks = RefreshSinks {
                    refreshed: &mut refreshed,
                    unchanged: &mut unchanged,
                    failed: &mut failed,
                    degradations: &mut degradations,
                };
                refresh_local_candidate(&scope, &output_scope, record, &fetched_at, &mut sinks)?;
            }
            Err(error) => {
                failed.push(selection_failure(record, error));
            }
        }
    }

    let index_status = if refreshed.is_empty() {
        IndexStatus::not_run()
    } else {
        match index::index_resolved_scope(&scope) {
            Ok(counts) => IndexStatus::indexed(IndexedCounts::from(counts)),
            Err(error) => {
                degradations.push(format!("index_failed:{error}"));
                IndexStatus::degraded()
            }
        }
    };

    Ok(render_refresh(RefreshRender {
        scope: output_scope,
        dry_run,
        planned,
        refreshed,
        unchanged,
        failed,
        skipped,
        index_status,
        degradations,
        explicit,
    }))
}

#[cfg(test)]
mod tests;

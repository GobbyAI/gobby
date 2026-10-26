//! Database half of the primary index. The plan says which files need parsing; the apply
//! writes the facts the node parsed and finishes the run. Every write is fenced to the
//! registered checkout, and the plan itself is read-only.

use std::collections::{HashMap, HashSet};
use std::time::Instant;

use anyhow::Context as _;
use postgres::Client;

use crate::config::Context;
use crate::index::api;
use crate::node::{FileFacts, FileHash, FinalBatch, ParsedFileFacts};

use super::file::{write_content_only_file_facts, write_parsed_file_facts};
use super::lifecycle::{
    attach_projection_sync, get_orphan_files, get_stale_files, refresh_communities,
    refresh_project_stats,
};
use super::local_imports::{
    resolve_local_import_calls, resolve_local_import_inheritance,
    resolve_project_local_import_calls,
};
use super::pipeline::ActiveIndexProgress;
use super::scan::{NodeScan, ScanKind};
use super::selector_blob::{self, HeadBlobs};
use super::sink::PostgresCodeFactSink;
use super::types::{IndexOptions, IndexOutcome, IndexRequest, IndexTarget};

/// The kind of run a plan or apply serves. It picks the stale set and what the run may
/// reconcile across the project.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RunScope {
    /// A whole-project run: reconciles orphans and advances the freshness watermark.
    Whole,
    /// A path-filtered run: reconciles only its subtree.
    Filtered,
    /// Explicit files: each file is judged alone, never reconciled as a project.
    Explicit,
}

impl RunScope {
    pub(crate) fn of(request: &IndexRequest) -> Self {
        if !request.explicit_files.is_empty() {
            Self::Explicit
        } else if request.path_filter.is_some() {
            Self::Filtered
        } else {
            Self::Whole
        }
    }

    /// The scope a wire request names. Explicit files do not travel on the wire yet.
    pub(crate) fn from_path_filter(path_filter: Option<&str>) -> Self {
        if path_filter.is_some() {
            Self::Filtered
        } else {
            Self::Whole
        }
    }

    fn reconciles_project(self) -> bool {
        self == Self::Whole
    }
}

/// What the plan asks the node to parse, and how many present files are stale.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PlanOutput {
    pub(crate) parse: Vec<String>,
    pub(crate) stale: usize,
}

/// What an apply left for the node. A non-empty `reparse` means the run is not complete and
/// the outcome is absent.
#[derive(Debug, Default)]
pub(crate) struct ApplyOutput {
    pub(crate) reparse: Vec<String>,
    /// Stale files whose existing facts were adopted, not parsed.
    pub(crate) adopted: Vec<String>,
    pub(crate) outcome: Option<IndexOutcome>,
}

/// Plans a run from the node's hashes. Writes nothing and takes no lock.
pub(crate) fn plan_primary(
    conn: &mut Client,
    ctx: &Context,
    scope: RunScope,
    full: bool,
    present: &[FileHash],
) -> anyhow::Result<PlanOutput> {
    if full {
        return Ok(PlanOutput {
            parse: present.iter().map(|file| file.path.clone()).collect(),
            stale: present.len(),
        });
    }
    let stale = stale_paths(conn, ctx, scope, present)?;
    let mut parse = Vec::new();
    for file in present.iter().filter(|file| stale.contains(&file.path)) {
        if !api::file_state::facts_adoptable(conn, &ctx.project_id, &file.path, &file.content_hash)?
        {
            parse.push(file.path.clone());
        }
    }
    Ok(PlanOutput {
        parse,
        stale: stale.len(),
    })
}

/// Writes the node's facts and, with the final batch, reconciles the rest of the run and
/// finishes it. The checkout fence runs before the first write.
pub(crate) fn apply_primary(
    conn: &mut Client,
    ctx: &Context,
    scope: RunScope,
    full: bool,
    files: &[ParsedFileFacts],
    batch: Option<&FinalBatch>,
) -> anyhow::Result<ApplyOutput> {
    let started = Instant::now();
    let project_id = ctx.project_id.as_str();
    let machine_id = ctx.machine_id.as_str();
    let root = ctx.project_root.as_path();
    api::upsert_project_seed(
        conn,
        machine_id,
        project_id,
        root,
        api::IndexWriteMode::Primary,
    )?;
    let Some(batch) = batch else {
        return apply_facts_only(conn, ctx, files);
    };

    let sent: HashSet<&str> = files
        .iter()
        .filter(|file| has_facts(file))
        .map(|file| file.path.as_str())
        .collect();
    let listed: HashSet<&str> = batch.indexed_paths.iter().map(String::as_str).collect();
    if sent != listed {
        anyhow::bail!(
            "final batch lists {} indexed paths but sent facts for {}",
            listed.len(),
            sent.len()
        );
    }

    let stale = if full {
        None
    } else {
        Some(stale_paths(conn, ctx, scope, &batch.present)?)
    };
    let stored = if full {
        api::file_state::SelectorStates::new()
    } else {
        api::file_state::selector_states(conn, machine_id, project_id)?
    };
    if scope.reconciles_project() {
        // A present file that could not be hashed is still present, so its state stays.
        let present: HashSet<String> = batch
            .present
            .iter()
            .map(|f| f.path.clone())
            .chain(
                files
                    .iter()
                    .filter(|file| matches!(file.facts, FileFacts::Unhashed))
                    .map(|file| file.path.clone()),
            )
            .collect();
        for orphan in get_orphan_files(conn, machine_id, project_id, &present)? {
            api::file_state::delete_file_state(
                conn,
                machine_id,
                project_id,
                &orphan,
                root,
                api::IndexWriteMode::Primary,
            )?;
        }
    }

    let mut outcome = IndexOutcome::new(project_id);
    for file in files {
        apply_file(conn, ctx, file, &mut outcome)?;
    }

    let handled: HashSet<&str> = files.iter().map(|file| file.path.as_str()).collect();
    let mut reparse = Vec::new();
    let mut adopted = Vec::new();
    for file in &batch.present {
        if handled.contains(file.path.as_str()) {
            continue;
        }
        match &stale {
            // A full run parses every present file, so an unhandled one must be parsed again.
            None => reparse.push(file.path.clone()),
            Some(stale) if !stale.contains(&file.path) => {
                record_blob(conn, machine_id, project_id, file, &stored)?;
                outcome.skipped_files += 1;
            }
            Some(_) => {
                if api::file_state::adopt_file_state(
                    conn,
                    machine_id,
                    project_id,
                    &file.path,
                    &file.content_hash,
                    root,
                    api::IndexWriteMode::Primary,
                )? {
                    record_blob(conn, machine_id, project_id, file, &stored)?;
                    adopted.push(file.path.clone());
                    outcome.skipped_files += 1;
                } else {
                    reparse.push(file.path.clone());
                }
            }
        }
    }
    if !reparse.is_empty() {
        return Ok(ApplyOutput {
            reparse,
            adopted,
            outcome: None,
        });
    }

    let post_started = Instant::now();
    outcome.scanned_files = batch.scanned_files;
    outcome.set_unsupported_file_types(batch.unsupported_file_types.clone());
    resolve_local_import_calls(conn, project_id, machine_id, &outcome.indexed_file_paths)?;
    if full && scope.reconciles_project() {
        resolve_project_local_import_calls(conn, project_id, machine_id)?;
    }
    let mut trigger_paths = outcome.indexed_file_paths.clone();
    trigger_paths.extend(adopted.iter().cloned());
    let owners = resolve_local_import_inheritance(conn, project_id, machine_id, &trigger_paths)?;
    outcome.record_promotion_owners(owners);
    outcome.durations.indexing_ms = started.elapsed().as_millis() as u64;

    let head_blobs = HeadBlobs::default();
    let target = IndexTarget {
        project_id,
        root_path: root,
        mode: api::IndexWriteMode::Primary,
        head_blobs: &head_blobs,
        machine_id,
    };
    let whole = scope.reconciles_project();
    refresh_project_stats(
        conn,
        machine_id,
        target,
        started.elapsed().as_millis() as u64,
        Some(batch.eligible_files),
        (full && whole).then_some(env!("CARGO_PKG_VERSION")),
        // A scoped run reconciles only its filter. Advancing the watermark would hide
        // unreconciled changes from the freshness pre-gate.
        whole,
    )?;
    outcome.durations.stats_ms = post_started.elapsed().as_millis() as u64;
    refresh_communities(conn, ctx, &mut outcome);
    Ok(ApplyOutput {
        reparse,
        adopted,
        outcome: Some(outcome),
    })
}

/// One primary index pass on `conn`. The node half scans and parses; this half plans, applies,
/// and finishes. Serves whole, filtered, and explicit runs, and an overlay's parent index.
pub(crate) fn index_with_connection(
    conn: &mut Client,
    request: &IndexRequest,
    ctx: &Context,
    options: &mut IndexOptions<'_>,
) -> anyhow::Result<IndexOutcome> {
    let started = Instant::now();
    let scope = RunScope::of(request);
    let mut scan = NodeScan::for_request(conn, request, ctx)?;
    let present = scan.file_hashes().to_vec();
    let plan = plan_primary(conn, ctx, scope, request.full, &present)?;
    let eligible = match scan.kind {
        ScanKind::Explicit => scan.routed,
        ScanKind::Discovered if request.full => scan.scanned,
        ScanKind::Discovered => plan.stale,
    };
    let discovery_ms = started.elapsed().as_millis() as u64;
    let mut progress = ActiveIndexProgress::new(options.progress.take(), eligible);

    let mut parse = plan.parse;
    parse.extend(scan.unhashed_to_parse(request.full));
    let mut facts = scan.preset();
    facts.extend(scan.unhashed_kept(request.full));
    facts.extend(scan.parse_paths(&parse)?);
    let mut applied = apply_primary(
        conn,
        ctx,
        scope,
        request.full,
        &facts,
        Some(&final_batch(&scan, &present, &facts, eligible)),
    )?;
    // A file the plan judged adoptable can lose its facts before the apply. Parse those paths
    // and apply every fact so far; each pass moves the reparsed paths into `facts`, so this ends.
    while !applied.reparse.is_empty() {
        facts.extend(scan.parse_paths(&applied.reparse)?);
        applied = apply_primary(
            conn,
            ctx,
            scope,
            request.full,
            &facts,
            Some(&final_batch(&scan, &present, &facts, eligible)),
        )?;
    }

    let mut outcome = applied
        .outcome
        .context("a finished apply carries its outcome")?;
    outcome.skipped_files += scan.extra_skipped;
    outcome.durations.discovery_ms = discovery_ms;
    for file in facts
        .iter()
        .filter(|file| !matches!(file.facts, FileFacts::Absent | FileFacts::Unhashed))
    {
        progress.advance(&file.path);
    }
    for path in &applied.adopted {
        progress.advance(path);
    }
    attach_projection_sync(&mut outcome, request);
    outcome.durations.total_ms = started.elapsed().as_millis() as u64;
    Ok(outcome)
}

/// Stale present paths for the run. Explicit runs treat every routed file as stale, because
/// each one is adopted or parsed on its own.
fn stale_paths(
    conn: &mut Client,
    ctx: &Context,
    scope: RunScope,
    present: &[FileHash],
) -> anyhow::Result<HashSet<String>> {
    if scope == RunScope::Explicit {
        return Ok(present.iter().map(|file| file.path.clone()).collect());
    }
    let hashes: HashMap<String, String> = present
        .iter()
        .map(|file| (file.path.clone(), file.content_hash.clone()))
        .collect();
    get_stale_files(conn, &ctx.machine_id, &ctx.project_id, &hashes)
}

fn final_batch(
    scan: &NodeScan,
    present: &[FileHash],
    facts: &[ParsedFileFacts],
    eligible: usize,
) -> FinalBatch {
    FinalBatch {
        present: present.to_vec(),
        indexed_paths: facts
            .iter()
            .filter(|file| has_facts(file))
            .map(|file| file.path.clone())
            .collect(),
        eligible_files: eligible,
        scanned_files: scan.scanned,
        unsupported_file_types: scan.unsupported.clone(),
    }
}

fn has_facts(file: &ParsedFileFacts) -> bool {
    matches!(
        file.facts,
        FileFacts::Parsed { .. } | FileFacts::ContentOnly { .. }
    )
}

/// The apply path without a final batch: the facts are written and nothing is reconciled.
fn apply_facts_only(
    conn: &mut Client,
    ctx: &Context,
    files: &[ParsedFileFacts],
) -> anyhow::Result<ApplyOutput> {
    let mut outcome = IndexOutcome::new(ctx.project_id.as_str());
    for file in files {
        apply_file(conn, ctx, file, &mut outcome)?;
    }
    Ok(ApplyOutput {
        reparse: Vec::new(),
        adopted: Vec::new(),
        outcome: None,
    })
}

/// Writes one file's outcome in its own transaction. Facts and their state row commit together.
fn apply_file(
    conn: &mut Client,
    ctx: &Context,
    file: &ParsedFileFacts,
    outcome: &mut IndexOutcome,
) -> anyhow::Result<()> {
    let project_id = ctx.project_id.as_str();
    let machine_id = ctx.machine_id.as_str();
    let root = ctx.project_root.as_path();
    let rel = file.path.as_str();
    match &file.facts {
        FileFacts::Parsed {
            language,
            content_hash,
            byte_size,
            git_blob,
            facts,
        } => {
            let mut tx = conn
                .transaction()
                .context("start indexed file transaction")?;
            let mut sink = PostgresCodeFactSink::new(
                &mut tx,
                project_id,
                root,
                api::IndexWriteMode::Primary,
                machine_id,
            )?;
            let counts = write_parsed_file_facts(
                &mut sink,
                project_id,
                rel,
                language,
                content_hash,
                *byte_size,
                facts,
            )?;
            selector_blob::record_selector_blob(
                &mut tx,
                machine_id,
                project_id,
                rel,
                content_hash,
                git_blob.as_deref(),
            )?;
            tx.commit().context("commit indexed file transaction")?;
            outcome.add_counts(counts);
        }
        FileFacts::ContentOnly {
            language,
            content_hash,
            byte_size,
            git_blob,
            chunks,
        } => {
            let mut tx = conn
                .transaction()
                .context("start content-only file transaction")?;
            let mut sink = PostgresCodeFactSink::new(
                &mut tx,
                project_id,
                root,
                api::IndexWriteMode::Primary,
                machine_id,
            )?;
            let counts = write_content_only_file_facts(
                &mut sink,
                project_id,
                rel,
                language,
                content_hash,
                *byte_size,
                chunks,
            )?;
            selector_blob::record_selector_blob(
                &mut tx,
                machine_id,
                project_id,
                rel,
                content_hash,
                git_blob.as_deref(),
            )?;
            tx.commit()
                .context("commit content-only file transaction")?;
            outcome.add_counts(counts);
        }
        FileFacts::Skipped => outcome.skipped_files += 1,
        FileFacts::Removed => {
            api::file_state::delete_file_state(
                conn,
                machine_id,
                project_id,
                rel,
                root,
                api::IndexWriteMode::Primary,
            )?;
            outcome.skipped_files += 1;
        }
        // The file is present: its state stays and nothing is counted.
        FileFacts::Unhashed => {}
        FileFacts::Absent => {
            api::file_state::delete_file_state(
                conn,
                machine_id,
                project_id,
                rel,
                root,
                api::IndexWriteMode::Primary,
            )?;
        }
    }
    Ok(())
}

/// Records a file's Git blob when the stored selector differs. An unchanged selector is not
/// written.
fn record_blob(
    conn: &mut Client,
    machine_id: &str,
    project_id: &str,
    file: &FileHash,
    stored: &api::file_state::SelectorStates,
) -> anyhow::Result<()> {
    let blob = file.git_blob.as_deref();
    let unchanged = stored.get(&file.path).is_some_and(|(hash, stored_blob)| {
        *hash == file.content_hash && stored_blob.as_deref() == blob
    });
    if unchanged {
        return Ok(());
    }
    selector_blob::record_selector_blob(
        conn,
        machine_id,
        project_id,
        &file.path,
        &file.content_hash,
        blob,
    )
}

//! Read-only inventory of the code-symbol vector projection against PostgreSQL.
//!
//! The dry run scrolls the project's Qdrant collection and reads PostgreSQL. It
//! writes nothing and deletes nothing. Orphan classes are evidence only.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

use anyhow::Context as _;
use postgres::GenericClient;

use crate::commands::receipt;
use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, Context};
use crate::db;
use crate::index::api::IndexDegradation;
use crate::index_lock::{IndexLockPolicy, lease_project_lock};
use crate::models::{CODE_INDEX_UUID_NAMESPACE, Symbol};
use crate::output::{self, Format};
use crate::vector::code_symbols::{
    self, CodeSymbolVectorLifecycle, PointIdentity, VectorLifecycleError,
};

mod pins;

#[cfg(test)]
#[path = "reconcile/tests.rs"]
mod tests;

/// Why a point ID absent from PostgreSQL is an orphan. The first matching class wins.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum OrphanClass {
    LegacyPayload,
    LegacyIdScheme,
    ReparsedVersion,
    Unattributed,
}

const ORPHAN_CLASSES: [OrphanClass; 4] = [
    OrphanClass::LegacyPayload,
    OrphanClass::LegacyIdScheme,
    OrphanClass::ReparsedVersion,
    OrphanClass::Unattributed,
];

impl OrphanClass {
    fn as_str(self) -> &'static str {
        match self {
            Self::LegacyPayload => "legacy_payload",
            Self::LegacyIdScheme => "legacy_id_scheme",
            Self::ReparsedVersion => "reparsed_version",
            Self::Unattributed => "unattributed",
        }
    }
}

/// Why a version has symbols whose points are absent from Qdrant.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum MissingClass {
    SyncedMissingCurrent,
    SyncedMissingRetained,
    PendingCurrent,
    PendingRetained,
}

const MISSING_CLASSES: [MissingClass; 4] = [
    MissingClass::SyncedMissingCurrent,
    MissingClass::SyncedMissingRetained,
    MissingClass::PendingCurrent,
    MissingClass::PendingRetained,
];

impl MissingClass {
    fn as_str(self) -> &'static str {
        match self {
            Self::SyncedMissingCurrent => "synced_missing_current",
            Self::SyncedMissingRetained => "synced_missing_retained",
            Self::PendingCurrent => "pending_current",
            Self::PendingRetained => "pending_retained",
        }
    }
}

/// Versions in one missing class, with the count of their absent symbol points.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct MissingGroup {
    pub(crate) version_ids: Vec<String>,
    pub(crate) symbols: usize,
}

/// The full dry-run inventory for one project.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Inventory {
    pub(crate) points_scanned: usize,
    pub(crate) pg_symbols: usize,
    pub(crate) orphans: BTreeMap<OrphanClass, Vec<String>>,
    pub(crate) missing: BTreeMap<MissingClass, MissingGroup>,
    pub(crate) inventory_digest: String,
}

/// One `code_indexed_files` row of the project.
struct VersionRow {
    id: String,
    file_path: String,
    content_hash: String,
    synced: bool,
}

pub(crate) fn inventory(ctx: &Context) -> anyhow::Result<Inventory> {
    inventory_in_scope(ctx, None)
}

/// Classifies the project's vectors. `scope` limits the Qdrant scroll and every
/// PostgreSQL read to those file paths; `None` covers the whole project.
pub(crate) fn inventory_in_scope(
    ctx: &Context,
    scope: Option<&[String]>,
) -> anyhow::Result<Inventory> {
    let qdrant = ctx
        .qdrant
        .as_ref()
        .ok_or(VectorLifecycleError::MissingQdrantConfig)?;
    // Scroll Qdrant before reading PostgreSQL. A point present at scroll time was
    // projected from rows written earlier, so the later read still sees its symbol
    // unless that symbol was removed.
    let points = code_symbols::scroll_point_identities(qdrant, &ctx.project_id, scope)?;
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let project = db::id_param(&ctx.project_id)?;
    // NULL scope reads every file; a list reads only those file paths.
    let scope_paths: Option<Vec<String>> = scope.map(<[String]>::to_vec);

    let symbol_rows = conn.query(
        "SELECT id::text, file_path, file_content_hash FROM code_symbols \
         WHERE project_id = $1 AND ($2::text[] IS NULL OR file_path = ANY($2))",
        &[&project, &scope_paths],
    )?;
    let version_rows = conn.query(
        "SELECT id::text, file_path, content_hash, vectors_synced \
         FROM code_indexed_files WHERE project_id = $1 \
         AND ($2::text[] IS NULL OR file_path = ANY($2))",
        &[&project, &scope_paths],
    )?;
    // Any machine's reference keeps a version; the dry run does not filter by machine.
    let reference_rows = conn.query(
        "SELECT DISTINCT file_path, content_hash FROM code_indexed_file_states \
         WHERE project_id = $1 AND ($2::text[] IS NULL OR file_path = ANY($2))",
        &[&project, &scope_paths],
    )?;

    let mut pg_ids: HashSet<String> = HashSet::new();
    let mut symbols_by_version: HashMap<(String, String), Vec<String>> = HashMap::new();
    for row in &symbol_rows {
        let id: String = row.get(0);
        let path: String = row.get(1);
        // Legacy symbol rows predate file_content_hash and may be NULL.
        let hash: Option<String> = row.get(2);
        pg_ids.insert(id.clone());
        if let Some(hash) = hash {
            symbols_by_version.entry((path, hash)).or_default().push(id);
        }
    }

    let versions: Vec<VersionRow> = version_rows
        .iter()
        .map(|row| VersionRow {
            id: row.get(0),
            file_path: row.get(1),
            content_hash: row.get(2),
            synced: row.get(3),
        })
        .collect();
    let references: HashSet<(String, String)> = reference_rows
        .iter()
        .map(|row| (row.get(0), row.get(1)))
        .collect();

    let mut hashes_by_path: HashMap<String, Vec<String>> = HashMap::new();
    for version in &versions {
        hashes_by_path
            .entry(version.file_path.clone())
            .or_default()
            .push(version.content_hash.clone());
    }

    let point_ids: HashSet<&str> = points.iter().map(|point| point.id.as_str()).collect();
    let pinned_ids = pins::pinned_keep_ids(&mut conn, ctx, scope)?;
    let mut orphans: BTreeMap<OrphanClass, Vec<String>> = ORPHAN_CLASSES
        .into_iter()
        .map(|class| (class, Vec::new()))
        .collect();
    for point in &points {
        if !pg_ids.contains(point.id.as_str()) && !pinned_ids.contains(point.id.as_str()) {
            let class = classify_orphan(&ctx.project_id, point, &hashes_by_path);
            orphans.entry(class).or_default().push(point.id.clone());
        }
    }

    let mut missing: BTreeMap<MissingClass, MissingGroup> = MISSING_CLASSES
        .into_iter()
        .map(|class| (class, MissingGroup::default()))
        .collect();
    for version in &versions {
        let key = (version.file_path.clone(), version.content_hash.clone());
        let Some(symbol_ids) = symbols_by_version.get(&key) else {
            continue;
        };
        let absent = symbol_ids
            .iter()
            .filter(|id| !point_ids.contains(id.as_str()))
            .count();
        if absent == 0 {
            continue;
        }
        let referenced = references.contains(&key);
        let class = match (version.synced, referenced) {
            (true, true) => MissingClass::SyncedMissingCurrent,
            (true, false) => MissingClass::SyncedMissingRetained,
            (false, true) => MissingClass::PendingCurrent,
            (false, false) => MissingClass::PendingRetained,
        };
        let group = missing.entry(class).or_default();
        group.version_ids.push(version.id.clone());
        group.symbols += absent;
    }

    let orphan_ids: Vec<String> = orphans.values().flatten().cloned().collect();
    let synced_missing = synced_missing_ids(&missing);
    let digest = inventory_digest(&orphan_ids, &synced_missing);

    Ok(Inventory {
        points_scanned: points.len(),
        pg_symbols: pg_ids.len(),
        orphans,
        missing,
        inventory_digest: digest,
    })
}

/// Assigns a point absent from PostgreSQL to the first orphan class that matches.
fn classify_orphan(
    project_id: &str,
    point: &PointIdentity,
    hashes_by_path: &HashMap<String, Vec<String>>,
) -> OrphanClass {
    let Some(file_path) = point.file_path.as_deref() else {
        return OrphanClass::LegacyPayload;
    };
    let (Some(name), Some(kind), Some(byte_start)) = (
        point.name.as_deref(),
        point.kind.as_deref(),
        point.byte_start,
    ) else {
        return OrphanClass::Unattributed;
    };
    let legacy_key = format!("{project_id}:{file_path}:{name}:{kind}:{byte_start}");
    if Uuid::new_v5(&CODE_INDEX_UUID_NAMESPACE, legacy_key.as_bytes()).to_string() == point.id {
        return OrphanClass::LegacyIdScheme;
    }
    let reparsed = hashes_by_path
        .get(file_path)
        .into_iter()
        .flatten()
        .any(|hash| {
            Symbol::make_id(project_id, file_path, hash, name, kind, byte_start) == point.id
        });
    if reparsed {
        OrphanClass::ReparsedVersion
    } else {
        OrphanClass::Unattributed
    }
}

/// `sha256:` plus the hex digest of the sorted orphan IDs, a `--` line, and the
/// sorted synced-missing version IDs. Each ID is newline-terminated.
pub(crate) fn inventory_digest(
    orphan_ids: &[String],
    synced_missing_version_ids: &[String],
) -> String {
    let mut material = String::new();
    push_sorted_lines(&mut material, orphan_ids);
    material.push_str("--\n");
    push_sorted_lines(&mut material, synced_missing_version_ids);
    format!(
        "sha256:{}",
        gobby_core::indexing::content_hash(material.as_bytes())
    )
}

fn push_sorted_lines(material: &mut String, ids: &[String]) {
    let mut sorted: Vec<&str> = ids.iter().map(String::as_str).collect();
    sorted.sort_unstable();
    for id in sorted {
        material.push_str(id);
        material.push('\n');
    }
}

pub(crate) fn run(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let report = inventory(ctx)?;
    let collection = code_symbols::collection_name(CODE_SYMBOL_COLLECTION_PREFIX, &ctx.project_id)?;
    match format {
        Format::Json => output::print_json(&json_report(&ctx.project_id, &collection, &report)),
        Format::Text => output::print_text(&text_report(&collection, &report)),
    }
}

/// Rows per Qdrant delete request, matching retire-files' projection batch size.
const DELETE_BATCH_SIZE: usize = 256;

pub(crate) fn run_command(
    ctx: &Context,
    apply_requested: bool,
    expect_digest: Option<&str>,
    receipt_path: Option<&Path>,
    format: Format,
) -> anyhow::Result<()> {
    if !apply_requested {
        return run(ctx, format);
    }
    let (Some(expect_digest), Some(receipt_path)) = (expect_digest, receipt_path) else {
        anyhow::bail!("--apply requires --expect-digest and --receipt");
    };
    let applied = apply(ctx, expect_digest, receipt_path)?;
    let collection = code_symbols::collection_name(CODE_SYMBOL_COLLECTION_PREFIX, &ctx.project_id)?;
    match format {
        Format::Json => output::print_json(&json_apply_report(
            &ctx.project_id,
            &collection,
            receipt_path,
            &applied,
        )),
        Format::Text => output::print_text(&text_apply_report(&collection, receipt_path, &applied)),
    }
}

fn json_report(project_id: &str, collection: &str, report: &Inventory) -> Value {
    let orphans: serde_json::Map<String, Value> = report
        .orphans
        .iter()
        .map(|(class, ids)| {
            (
                class.as_str().to_string(),
                json!({"count": ids.len(), "ids": ids}),
            )
        })
        .collect();
    let missing: serde_json::Map<String, Value> = report
        .missing
        .iter()
        .map(|(class, group)| {
            (
                class.as_str().to_string(),
                json!({
                    "versions": group.version_ids.len(),
                    "symbols": group.symbols,
                    "version_ids": group.version_ids,
                }),
            )
        })
        .collect();
    json!({
        "project_id": project_id,
        "collection": collection,
        "mode": "dry_run",
        "success": true,
        "points_scanned": report.points_scanned,
        "pg_symbols": report.pg_symbols,
        "orphans": orphans,
        "missing": missing,
        "inventory_digest": report.inventory_digest,
    })
}

/// Receipt for one guarded apply run. Planned fields are written before any mutation;
/// confirmed fields change only after the mutation they name has committed.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct ReconcileReceipt {
    pub(crate) digest: String,
    pub(crate) project_id: String,
    pub(crate) collection: String,
    pub(crate) planned_orphans: BTreeMap<String, Vec<String>>,
    pub(crate) planned_versions_reset: Vec<String>,
    pub(crate) versions_reset: Vec<String>,
    pub(crate) deleted: Vec<Vec<String>>,
    pub(crate) complete: bool,
}

pub(crate) fn apply(
    ctx: &Context,
    expect_digest: &str,
    receipt_path: &Path,
) -> anyhow::Result<ReconcileReceipt> {
    apply_with_persist(ctx, expect_digest, receipt_path, &mut |receipt| {
        receipt::write_atomic(receipt_path, receipt)
    })
}

pub(crate) fn apply_with_persist(
    ctx: &Context,
    expect_digest: &str,
    receipt_path: &Path,
    persist: &mut dyn FnMut(&ReconcileReceipt) -> anyhow::Result<()>,
) -> anyhow::Result<ReconcileReceipt> {
    receipt::validate_location(receipt_path, &ctx.project_root)?;
    let qdrant = ctx
        .qdrant
        .as_ref()
        .ok_or(VectorLifecycleError::MissingQdrantConfig)?;

    // The lease connection holds the project lock for the whole run. Writes use their
    // own connection, so the reset transaction does not borrow the lease.
    let mut lease_conn = db::connect_readwrite(&ctx.database_url)?;
    let _lease = lease_project_lock(
        &mut lease_conn,
        &ctx.project_id,
        IndexLockPolicy::maintenance_try(),
    )?
    .ok_or_else(|| anyhow::anyhow!("project index is busy; rerun the dry-run"))?;

    let report = inventory(ctx)?;
    anyhow::ensure!(
        report.inventory_digest == expect_digest,
        "inventory changed since the dry run: expected {expect_digest}, found {}",
        report.inventory_digest,
    );
    let collection = code_symbols::collection_name(CODE_SYMBOL_COLLECTION_PREFIX, &ctx.project_id)?;
    let planned_versions_reset = synced_missing_ids(&report.missing);
    let orphan_ids: Vec<String> = report.orphans.values().flatten().cloned().collect();
    let mut progress = ReconcileReceipt {
        digest: report.inventory_digest.clone(),
        project_id: ctx.project_id.clone(),
        collection: collection.clone(),
        planned_orphans: report
            .orphans
            .iter()
            .map(|(class, ids)| (class.as_str().to_string(), ids.clone()))
            .collect(),
        planned_versions_reset: planned_versions_reset.clone(),
        versions_reset: Vec::new(),
        deleted: Vec::new(),
        complete: false,
    };

    if let Some(previous) = receipt::read_existing::<ReconcileReceipt>(receipt_path)? {
        anyhow::ensure!(
            previous.project_id == progress.project_id
                && previous.collection == progress.collection
                && previous.digest == progress.digest,
            "receipt {} belongs to another reconcile run",
            receipt_path.display(),
        );
    }
    persist(&progress)
        .with_context(|| format!("cannot write receipt {}", receipt_path.display()))?;

    let versions = db::id_params(&planned_versions_reset)?;
    let mut writer = db::connect_readwrite(&ctx.database_url)?;
    let mut tx = writer.transaction()?;
    let reset = reset_synced_versions(&mut tx, versions)?;
    if let Err(error) = tx.commit() {
        return Err(unconfirmed(
            receipt_path,
            &progress,
            anyhow::Error::from(error),
        ));
    }
    progress.versions_reset = reset;
    persist(&progress).map_err(|error| unconfirmed(receipt_path, &progress, error))?;

    for batch in orphan_ids.chunks(DELETE_BATCH_SIZE) {
        if let Err(error) = code_symbols::delete_symbol_vectors(qdrant, &ctx.project_id, batch) {
            return Err(anyhow::Error::from(error).context(format!(
                "Qdrant delete failed; receipt {} is partial",
                receipt_path.display()
            )));
        }
        progress.deleted.push(batch.to_vec());
        persist(&progress).map_err(|error| unconfirmed(receipt_path, &progress, error))?;
    }

    progress.complete = true;
    persist(&progress).map_err(|error| unconfirmed(receipt_path, &progress, error))?;
    Ok(progress)
}

/// Reports a receipt write that follows a committed mutation. The publish may or may not
/// have landed, so the run exits nonzero and names the receipt and the confirmed work.
fn unconfirmed(path: &Path, progress: &ReconcileReceipt, error: anyhow::Error) -> anyhow::Error {
    let deleted: Vec<&str> = progress
        .deleted
        .iter()
        .flatten()
        .map(String::as_str)
        .collect();
    anyhow::anyhow!(
        "receipt write unconfirmed: {}\nversions_reset: {}\ndeleted: {}\ncause: {error:#}",
        path.display(),
        progress.versions_reset.len(),
        deleted.join(" "),
    )
}

fn synced_missing_ids(missing: &BTreeMap<MissingClass, MissingGroup>) -> Vec<String> {
    [
        MissingClass::SyncedMissingCurrent,
        MissingClass::SyncedMissingRetained,
    ]
    .iter()
    .filter_map(|class| missing.get(class))
    .flat_map(|group| group.version_ids.iter().cloned())
    .collect()
}

fn json_apply_report(
    project_id: &str,
    collection: &str,
    receipt_path: &Path,
    applied: &ReconcileReceipt,
) -> Value {
    json!({
        "project_id": project_id,
        "collection": collection,
        "mode": "apply",
        "success": true,
        "inventory_digest": applied.digest,
        "receipt": receipt_path.display().to_string(),
        "deleted_points": applied.deleted.iter().map(Vec::len).sum::<usize>(),
        "versions_reset": applied.versions_reset.len(),
        "complete": applied.complete,
    })
}

fn text_apply_report(collection: &str, receipt_path: &Path, applied: &ReconcileReceipt) -> String {
    [
        format!(
            "vector reconcile apply for {collection}: complete {}",
            applied.complete
        ),
        format!(
            "deleted points: {}",
            applied.deleted.iter().map(Vec::len).sum::<usize>()
        ),
        format!("versions reset: {}", applied.versions_reset.len()),
        format!("receipt: {}", receipt_path.display()),
    ]
    .join("\n")
}

fn text_report(collection: &str, report: &Inventory) -> String {
    let mut lines = vec![
        format!("vector reconcile dry run for {collection}: nothing deleted"),
        format!("points scanned: {}", report.points_scanned),
        format!("postgres symbols: {}", report.pg_symbols),
    ];
    lines.extend(
        report
            .orphans
            .iter()
            .map(|(class, ids)| format!("orphan {}: {}", class.as_str(), ids.len())),
    );
    lines.extend(report.missing.iter().map(|(class, group)| {
        format!(
            "missing {}: {} versions, {} symbols",
            class.as_str(),
            group.version_ids.len(),
            group.symbols
        )
    }));
    lines.push(format!("inventory digest: {}", report.inventory_digest));
    lines.join("\n")
}

/// Resets the flag on each synced version that lost a symbol point, so the pending-sync
/// worker re-embeds it. Orphan points are left alone; deletion stays with `apply`.
pub(crate) fn reset_synced_missing_versions(
    ctx: &Context,
    scope: Option<&[String]>,
) -> anyhow::Result<Vec<String>> {
    let report = inventory_in_scope(ctx, scope)?;
    let versions = db::id_params(&synced_missing_ids(&report.missing))?;
    let mut writer = db::connect_readwrite(&ctx.database_url)?;
    reset_synced_versions(&mut writer, versions)
}

/// Clears `vectors_synced` on each named version that is still synced. The reconcile
/// apply path and the automatic repair share it, so both reset the same rows.
fn reset_synced_versions(
    conn: &mut impl GenericClient,
    versions: Vec<Uuid>,
) -> anyhow::Result<Vec<String>> {
    let rows = conn.query(
        "UPDATE code_indexed_files SET vectors_synced = false, vector_sync_attempted_at = NULL \
         WHERE id = ANY($1) AND vectors_synced RETURNING id::text",
        &[&versions],
    )?;
    Ok(rows.iter().map(|row| row.get(0)).collect())
}

/// Runs the automatic repair after an index pass. A version whose symbol points are
/// missing is reset here; the pending-sync worker then re-embeds it. A projected pin
/// whose overlay points are missing is re-projected here, because the pending selector
/// would not revisit it until its source moves.
pub(crate) fn repair_after_index(
    ctx: &Context,
    scope: Option<&[String]>,
) -> Option<IndexDegradation> {
    ctx.qdrant.as_ref()?;
    let repaired = reset_synced_missing_versions(ctx, scope)
        .map(drop)
        .and_then(|()| {
            repair_missing_pins(ctx, scope, || {
                crate::projection::sync::vector_lifecycle_from_context(ctx)
            })
        });
    match repaired {
        Ok(()) => None,
        Err(error) => Some(IndexDegradation::VectorRepairFailed {
            message: format!("{error:#}"),
        }),
    }
}

/// Re-projects the projected pins whose overlay points are missing or stale, with the
/// lifecycle the caller supplies. A degraded report fails the repair so the index pass
/// records it.
pub(crate) fn repair_missing_pins(
    ctx: &Context,
    scope: Option<&[String]>,
    lifecycle: impl FnOnce() -> Result<CodeSymbolVectorLifecycle, VectorLifecycleError>,
) -> anyhow::Result<()> {
    let paths = pins::drifted_pin_paths(ctx, scope)?;
    if paths.is_empty() {
        return Ok(());
    }
    let report =
        crate::projection::sync::sync_vector_files_with_lifecycle(ctx, lifecycle()?, &paths, None)?;
    if let Some(error) = report.error {
        anyhow::bail!("pinned vector repair: {}: {}", error.kind, error.message);
    }
    if report.failed_files > 0 {
        anyhow::bail!(
            "pinned vector repair: {} file(s) failed to re-project",
            report.failed_files
        );
    }
    Ok(())
}

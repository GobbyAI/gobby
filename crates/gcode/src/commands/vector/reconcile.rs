//! Read-only inventory of the code-symbol vector projection against PostgreSQL.
//!
//! The dry run scrolls the project's Qdrant collection and reads PostgreSQL. It
//! writes nothing and deletes nothing. Orphan classes are evidence only.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, Context};
use crate::db;
use crate::models::{CODE_INDEX_UUID_NAMESPACE, Symbol};
use crate::output::{self, Format};
use crate::vector::code_symbols::{self, PointIdentity, VectorLifecycleError};

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
    let qdrant = ctx
        .qdrant
        .as_ref()
        .ok_or(VectorLifecycleError::MissingQdrantConfig)?;
    // Scroll Qdrant before reading PostgreSQL. A point present at scroll time was
    // projected from rows written earlier, so the later read still sees its symbol
    // unless that symbol was removed.
    let points = code_symbols::scroll_point_identities(qdrant, &ctx.project_id)?;
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let project = db::id_param(&ctx.project_id)?;

    let symbol_rows = conn.query(
        "SELECT id::text, file_path, file_content_hash FROM code_symbols WHERE project_id = $1",
        &[&project],
    )?;
    let version_rows = conn.query(
        "SELECT id::text, file_path, content_hash, vectors_synced \
         FROM code_indexed_files WHERE project_id = $1",
        &[&project],
    )?;
    // Any machine's reference keeps a version; the dry run does not filter by machine.
    let reference_rows = conn.query(
        "SELECT DISTINCT file_path, content_hash FROM code_indexed_file_states \
         WHERE project_id = $1",
        &[&project],
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
    let mut orphans: BTreeMap<OrphanClass, Vec<String>> = ORPHAN_CLASSES
        .into_iter()
        .map(|class| (class, Vec::new()))
        .collect();
    for point in &points {
        if !pg_ids.contains(point.id.as_str()) {
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
    let synced_missing: Vec<String> = [
        MissingClass::SyncedMissingCurrent,
        MissingClass::SyncedMissingRetained,
    ]
    .iter()
    .filter_map(|class| missing.get(class))
    .flat_map(|group| group.version_ids.iter().cloned())
    .collect();
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

pub(crate) fn run_command(
    ctx: &Context,
    apply: bool,
    expect_digest: Option<&str>,
    receipt: Option<&Path>,
    format: Format,
) -> anyhow::Result<()> {
    let _ = (expect_digest, receipt);
    if apply {
        anyhow::bail!("reconcile apply is not implemented");
    }
    run(ctx, format)
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
    let _ = (ctx, expect_digest, receipt_path);
    anyhow::bail!("reconcile apply is not implemented")
}

pub(crate) fn apply_with_persist(
    ctx: &Context,
    expect_digest: &str,
    receipt_path: &Path,
    persist: &mut dyn FnMut(&ReconcileReceipt) -> anyhow::Result<()>,
) -> anyhow::Result<ReconcileReceipt> {
    let _ = (ctx, expect_digest, receipt_path, persist);
    anyhow::bail!("reconcile apply is not implemented")
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

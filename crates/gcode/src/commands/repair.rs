use std::collections::BTreeSet;
use std::time::Instant;

use serde::Serialize;

use crate::commands::index::{self, IndexSyncProjectionsOutput, RunIndexLockedOutput};
use crate::config::Context;
use crate::db;
use crate::graph::code_graph::{GraphFileHashRead, GraphFileHashes, read_project_file_hashes};
use crate::index::api::{self, GraphSyncedFile};
use crate::index::indexer::{
    IndexRequest, LocalImportRepair, resolve_project_local_import_calls,
    resolve_project_local_import_inheritance,
};
use crate::index_lock::{self, IndexLockPolicy, IndexLockResult};
use crate::output::{self, Format};
use crate::projection::sync::{ProjectionStatus, ProjectionSyncReport};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
enum RepairMode {
    FullReindex,
    Repair,
}

impl RepairMode {
    fn as_str(self) -> &'static str {
        match self {
            Self::FullReindex => "full_reindex",
            Self::Repair => "repair",
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
struct LocalImportCallsSummary {
    pending: usize,
    resolved: usize,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
struct LocalImportInheritanceSummary {
    pending: usize,
    resolved: usize,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
struct MarkedForResyncSummary {
    promotion_owners: usize,
    graph_drift: usize,
    total: usize,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
struct GraphReconcileSummary {
    checked: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    skipped_reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct RepairOutput {
    project_id: String,
    mode: RepairMode,
    previous_indexer_version: Option<String>,
    indexer_version: String,
    local_import_calls: LocalImportCallsSummary,
    local_import_inheritance: LocalImportInheritanceSummary,
    marked_for_resync: MarkedForResyncSummary,
    graph_reconcile: GraphReconcileSummary,
    /// Index and projection report of the version-gated full run; absent in
    /// `repair` mode. A degraded graph or vector sync is only visible here.
    #[serde(skip_serializing_if = "Option::is_none")]
    full_reindex: Option<IndexSyncProjectionsOutput>,
    duration_ms: u64,
}

pub fn run(ctx: &Context, format: Format) -> anyhow::Result<()> {
    let started = Instant::now();
    let result =
        index_lock::with_project_lock(ctx, IndexLockPolicy::wait(), || run_locked(ctx, started))?;
    let output = match result {
        IndexLockResult::Acquired(output) => output,
        IndexLockResult::Busy(holder) => anyhow::bail!(
            "index lock is busy for project {}; wait policy did not acquire it{}",
            ctx.project_id,
            holder
                .map(|holder| format!("; {holder}"))
                .unwrap_or_default()
        ),
    };
    match format {
        Format::Json => output::print_json(&output),
        Format::Text => output::print_text(&repair_text(&output)),
    }
}

fn run_locked(ctx: &Context, started: Instant) -> anyhow::Result<RepairOutput> {
    let current_version = env!("CARGO_PKG_VERSION");
    let machine_id = gobby_core::machine::read_local_machine_id()?;
    let mut conn = db::connect_readwrite(&ctx.database_url)?;
    let previous_version = api::project_indexer_version(&mut conn, &machine_id, &ctx.project_id)?;

    if needs_full_reindex(previous_version.as_deref(), current_version) {
        drop(conn);
        let full_reindex = match index::run_index_locked(
            ctx,
            IndexRequest {
                project_root: ctx.project_root.clone(),
                path_filter: None,
                explicit_files: Vec::new(),
                full: true,
                require_cpp_semantics: false,
                sync_projections: true,
            },
        )? {
            RunIndexLockedOutput::Projections(payload) => payload,
            RunIndexLockedOutput::IndexOnly(_) => {
                anyhow::bail!("full reindex returned no projection report")
            }
        };
        return Ok(RepairOutput {
            project_id: ctx.project_id.clone(),
            mode: RepairMode::FullReindex,
            previous_indexer_version: previous_version,
            indexer_version: current_version.to_string(),
            local_import_calls: LocalImportCallsSummary::default(),
            local_import_inheritance: LocalImportInheritanceSummary::default(),
            marked_for_resync: MarkedForResyncSummary::default(),
            graph_reconcile: GraphReconcileSummary::default(),
            full_reindex: Some(full_reindex),
            duration_ms: elapsed_ms(started),
        });
    }

    let calls = resolve_project_local_import_calls(&mut conn, &ctx.project_id)?;
    let inheritance = resolve_project_local_import_inheritance(&mut conn, &ctx.project_id)?;
    let promotion_owners = promotion_owners(&calls, &inheritance);
    let hub_files = api::graph_synced_files(&mut conn, &machine_id, &ctx.project_id)?;
    let (drift, graph_reconcile) = match read_project_file_hashes(ctx)? {
        GraphFileHashRead::Available(graph_files) => (
            graph_drift(&hub_files, &graph_files),
            GraphReconcileSummary {
                checked: hub_files.len(),
                skipped_reason: None,
            },
        ),
        GraphFileHashRead::Skipped { reason } => (
            Vec::new(),
            GraphReconcileSummary {
                checked: 0,
                skipped_reason: Some(reason),
            },
        ),
    };
    let paths = promotion_owners
        .iter()
        .cloned()
        .chain(drift.iter().cloned())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    api::mark_graph_unsynced(&mut conn, &machine_id, &ctx.project_id, &paths)?;

    Ok(RepairOutput {
        project_id: ctx.project_id.clone(),
        mode: RepairMode::Repair,
        previous_indexer_version: previous_version,
        indexer_version: current_version.to_string(),
        local_import_calls: LocalImportCallsSummary {
            pending: calls.pending,
            resolved: calls.resolved,
        },
        local_import_inheritance: LocalImportInheritanceSummary {
            pending: inheritance.pending,
            resolved: inheritance.resolved,
        },
        marked_for_resync: MarkedForResyncSummary {
            promotion_owners: promotion_owners.len(),
            graph_drift: drift.len(),
            total: paths.len(),
        },
        graph_reconcile,
        full_reindex: None,
        duration_ms: elapsed_ms(started),
    })
}

fn needs_full_reindex(stored: Option<&str>, current: &str) -> bool {
    stored != Some(current)
}

fn promotion_owners(
    calls: &LocalImportRepair,
    inheritance: &LocalImportRepair,
) -> BTreeSet<String> {
    calls
        .owners
        .iter()
        .chain(&inheritance.owners)
        .cloned()
        .collect()
}

fn graph_drift(hub: &[GraphSyncedFile], graph: &GraphFileHashes) -> Vec<String> {
    hub.iter()
        .filter(|file| {
            graph
                .get(&file.file_path)
                .is_none_or(|hashes| !hashes.contains(&file.content_hash))
        })
        .map(|file| file.file_path.clone())
        .collect()
}

fn elapsed_ms(started: Instant) -> u64 {
    u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX)
}

fn repair_text(output: &RepairOutput) -> String {
    let previous = output.previous_indexer_version.as_deref().unwrap_or("none");
    let skipped = output
        .graph_reconcile
        .skipped_reason
        .as_deref()
        .map(|reason| format!(", skipped={reason}"))
        .unwrap_or_default();
    let full = output
        .full_reindex
        .as_ref()
        .map(|payload| {
            format!(
                "\nfull reindex: indexed_files={}, skipped_files={}, degraded={}, graph={}, vector={}",
                payload.indexed_files,
                payload.skipped_files,
                payload.degraded.len(),
                projection_status_text(&payload.projections.graph),
                projection_status_text(&payload.projections.vector),
            )
        })
        .unwrap_or_default();
    format!(
        "project: {}\nmode: {}{full}\nindexer version: {} -> {}\nlocal import calls: pending={}, resolved={}\nlocal import inheritance: pending={}, resolved={}\nmarked for resync: promotion_owners={}, graph_drift={}, total={}\ngraph reconcile: checked={}{}\nduration: {} ms",
        output.project_id,
        output.mode.as_str(),
        previous,
        output.indexer_version,
        output.local_import_calls.pending,
        output.local_import_calls.resolved,
        output.local_import_inheritance.pending,
        output.local_import_inheritance.resolved,
        output.marked_for_resync.promotion_owners,
        output.marked_for_resync.graph_drift,
        output.marked_for_resync.total,
        output.graph_reconcile.checked,
        skipped,
        output.duration_ms,
    )
}

fn projection_status_text(report: &ProjectionSyncReport) -> String {
    let status = match report.status {
        ProjectionStatus::Ok => "ok",
        ProjectionStatus::Degraded => "degraded",
        ProjectionStatus::Failed => "failed",
    };
    match &report.error {
        Some(error) => format!("{status} ({})", error.kind),
        None => status.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::index::api::IndexOutcome;
    use crate::projection::sync::{ProjectionSyncError, ProjectionSyncReports};

    fn hub_file(path: &str, hash: &str) -> GraphSyncedFile {
        GraphSyncedFile {
            file_path: path.to_string(),
            content_hash: hash.to_string(),
        }
    }

    fn sample_output(
        mode: RepairMode,
        full_reindex: Option<IndexSyncProjectionsOutput>,
    ) -> RepairOutput {
        RepairOutput {
            project_id: "project-1".to_string(),
            mode,
            previous_indexer_version: None,
            indexer_version: "1.6.1".to_string(),
            local_import_calls: LocalImportCallsSummary {
                pending: 4,
                resolved: 3,
            },
            local_import_inheritance: LocalImportInheritanceSummary {
                pending: 2,
                resolved: 2,
            },
            marked_for_resync: MarkedForResyncSummary::default(),
            graph_reconcile: GraphReconcileSummary::default(),
            full_reindex,
            duration_ms: 7,
        }
    }

    fn projection_report(status: ProjectionStatus, error: Option<&str>) -> ProjectionSyncReport {
        ProjectionSyncReport {
            status,
            synced_files: 3,
            synced_symbols: 9,
            skipped_files: 0,
            failed_files: 0,
            degraded: error.is_some(),
            error: error.map(|kind| ProjectionSyncError {
                kind: kind.to_string(),
                message: "unavailable".to_string(),
            }),
        }
    }

    #[test]
    fn full_reindex_mode_reports_projection_outcome() {
        let payload = index::sync_projections_payload(
            &IndexOutcome {
                indexed_files: 3,
                symbols_indexed: 9,
                ..IndexOutcome::default()
            },
            ProjectionSyncReports {
                graph: projection_report(ProjectionStatus::Ok, None),
                vector: projection_report(ProjectionStatus::Failed, Some("missing_qdrant_config")),
            },
        );
        let output = sample_output(RepairMode::FullReindex, Some(payload));

        let json = serde_json::to_value(&output).expect("repair json");
        assert_eq!(json["mode"], "full_reindex");
        assert_eq!(json["full_reindex"]["indexed_files"], 3);
        assert_eq!(
            json["full_reindex"]["projections"]["vector"]["degraded"],
            true
        );
        assert_eq!(
            json["full_reindex"]["projections"]["vector"]["error"]["kind"],
            "missing_qdrant_config"
        );
        assert!(repair_text(&output).contains(
            "mode: full_reindex\nfull reindex: indexed_files=3, skipped_files=0, degraded=0, \
             graph=ok, vector=failed (missing_qdrant_config)\n"
        ));
    }

    #[test]
    fn repair_mode_omits_full_reindex_and_reports_inheritance_resolution() {
        let output = sample_output(RepairMode::Repair, None);

        let json = serde_json::to_value(&output).expect("repair json");
        assert!(json.get("full_reindex").is_none());
        assert_eq!(json["local_import_inheritance"]["resolved"], 2);
        let text = repair_text(&output);
        assert!(text.contains("local import inheritance: pending=2, resolved=2"));
        assert!(!text.contains("full reindex:"));
    }

    #[test]
    fn version_gate_requires_matching_indexer_version() {
        assert!(needs_full_reindex(None, "1.6.1"));
        assert!(needs_full_reindex(Some("1.6.0"), "1.6.1"));
        assert!(!needs_full_reindex(Some("1.6.1"), "1.6.1"));
    }

    #[test]
    fn graph_drift_finds_missing_and_stale_files() {
        let hub = [
            hub_file("missing.rs", "hash-a"),
            hub_file("stale.rs", "hash-b"),
        ];
        let graph = GraphFileHashes::from([(
            "stale.rs".to_string(),
            BTreeSet::from(["hash-old".to_string()]),
        )]);
        assert_eq!(
            graph_drift(&hub, &graph),
            vec!["missing.rs".to_string(), "stale.rs".to_string()]
        );
    }

    #[test]
    fn graph_drift_accepts_current_hash_among_old_versions() {
        let hub = [hub_file("current.rs", "hash-new")];
        let graph = GraphFileHashes::from([(
            "current.rs".to_string(),
            BTreeSet::from(["hash-old".to_string(), "hash-new".to_string()]),
        )]);
        assert!(graph_drift(&hub, &graph).is_empty());
    }
}

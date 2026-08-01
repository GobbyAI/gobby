use crate::graph::code_graph::GraphOrphanCleanup;
use crate::utils::short_id;
use crate::vector::code_symbols::VectorOrphanCleanup;

const ORPHAN_PROJECT_WARNING_LIMIT: usize = 5;
const ORPHAN_PROJECT_SUMMARY_LIMIT: usize = 8;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum SweepOutcome {
    Active,
    Deleted,
    AlreadyMissing,
    Busy,
}

#[derive(Debug, Default, PartialEq, Eq)]
pub(super) struct ReconcileTotals {
    pub(super) scanned: usize,
    pub(super) active: usize,
    pub(super) orphaned: usize,
    pub(super) deleted: usize,
    pub(super) already_missing: usize,
    pub(super) busy: usize,
    pub(super) invalid: usize,
    pub(super) failed: usize,
    pub(super) affected_ids: Vec<String>,
}

impl ReconcileTotals {
    pub(super) fn has_failures(&self) -> bool {
        self.failed > 0
    }

    pub(super) fn merge_mutation(&mut self, mutation: ReconcileTotals) {
        self.active += mutation.active;
        self.deleted += mutation.deleted;
        self.already_missing += mutation.already_missing;
        self.busy += mutation.busy;
        self.failed += mutation.failed;
        self.affected_ids.extend(mutation.affected_ids);
    }
}

pub(super) fn sweep_discovered_ids_with(
    project_ids: &[String],
    mut reconcile: impl FnMut(&str) -> anyhow::Result<SweepOutcome>,
) -> ReconcileTotals {
    let mut totals = ReconcileTotals {
        scanned: project_ids.len(),
        orphaned: project_ids.len(),
        ..ReconcileTotals::default()
    };
    for project_id in project_ids {
        match reconcile(project_id) {
            Ok(SweepOutcome::Active) => totals.active += 1,
            Ok(SweepOutcome::Deleted) => {
                totals.deleted += 1;
                totals.affected_ids.push(project_id.clone());
            }
            Ok(SweepOutcome::AlreadyMissing) => {
                totals.already_missing += 1;
                totals.affected_ids.push(project_id.clone());
            }
            Ok(SweepOutcome::Busy) => {
                totals.busy += 1;
                totals.affected_ids.push(project_id.clone());
            }
            Err(error) => {
                totals.failed += 1;
                totals.affected_ids.push(project_id.clone());
                eprintln!("Warning: reconciliation failed for {project_id}: {error:#}");
            }
        }
    }
    totals
}

#[derive(Default)]
pub(super) struct ProjectionPruneTotals {
    pub(super) graph_projects_cleaned: usize,
    pub(super) graph_projects_skipped: usize,
    pub(super) graph_stale_files_deleted: usize,
    pub(super) graph_nodes_deleted: usize,
    pub(super) vector_projects_cleaned: usize,
    pub(super) vector_projects_skipped: usize,
    pub(super) vector_orphan_files_deleted: usize,
    pub(super) vectors_deleted: usize,
}

impl ProjectionPruneTotals {
    pub(super) fn record_graph_cleanup(&mut self, cleanup: GraphOrphanCleanup) {
        self.graph_projects_cleaned += 1;
        self.graph_stale_files_deleted += cleanup.stale_files_deleted;
        self.graph_nodes_deleted += cleanup.graph_nodes_deleted;
    }

    pub(super) fn record_vector_cleanup(&mut self, cleanup: VectorOrphanCleanup) {
        self.vector_projects_cleaned += 1;
        self.vector_orphan_files_deleted += cleanup.orphan_files_deleted;
        self.vectors_deleted += cleanup.vectors_deleted;
    }

    pub(super) fn add(&mut self, other: ProjectionPruneTotals) {
        self.graph_projects_cleaned += other.graph_projects_cleaned;
        self.graph_projects_skipped += other.graph_projects_skipped;
        self.graph_stale_files_deleted += other.graph_stale_files_deleted;
        self.graph_nodes_deleted += other.graph_nodes_deleted;
        self.vector_projects_cleaned += other.vector_projects_cleaned;
        self.vector_projects_skipped += other.vector_projects_skipped;
        self.vector_orphan_files_deleted += other.vector_orphan_files_deleted;
        self.vectors_deleted += other.vectors_deleted;
    }
}

#[derive(Default, Debug, PartialEq, Eq)]
pub(in crate::commands::status) struct OrphanSqlDeletionCounts {
    pub(super) symbols_deleted: u64,
    pub(super) files_deleted: u64,
    pub(super) content_chunks_deleted: u64,
    pub(super) imports_deleted: u64,
    pub(super) calls_deleted: u64,
}

impl OrphanSqlDeletionCounts {
    pub(super) fn total(&self) -> u64 {
        self.symbols_deleted
            + self.files_deleted
            + self.content_chunks_deleted
            + self.imports_deleted
            + self.calls_deleted
    }
}

#[derive(Default)]
pub(super) struct OrphanProjectReconcileTotals {
    pub(super) project_ids: Vec<String>,
    pub(super) sql: OrphanSqlDeletionCounts,
    pub(super) graph_projects_cleared: usize,
    pub(super) graph_projects_skipped: usize,
    pub(super) vector_collections_deleted: usize,
    pub(super) vector_projects_skipped: usize,
}

impl OrphanProjectReconcileTotals {
    pub(super) fn record_sql(&mut self, project_id: String, counts: OrphanSqlDeletionCounts) {
        self.project_ids.push(project_id);
        self.sql.symbols_deleted += counts.symbols_deleted;
        self.sql.files_deleted += counts.files_deleted;
        self.sql.content_chunks_deleted += counts.content_chunks_deleted;
        self.sql.imports_deleted += counts.imports_deleted;
        self.sql.calls_deleted += counts.calls_deleted;
    }
}

pub(super) fn print_reconcile_totals(label: &str, totals: &ReconcileTotals) {
    for line in reconcile_totals_lines(label, totals) {
        eprintln!("{line}");
    }
}

pub(super) fn print_optional_reconcile_totals(
    label: &str,
    configured: bool,
    totals: Option<&ReconcileTotals>,
    orphan_buckets: Option<(usize, usize)>,
) {
    for line in optional_reconcile_totals_lines(label, configured, totals, orphan_buckets) {
        eprintln!("{line}");
    }
}

pub(super) fn reconcile_totals_lines(label: &str, totals: &ReconcileTotals) -> Vec<String> {
    let mut lines = vec![format!(
        "{label}: scanned={}, active={}, orphaned={}, deleted={}, already_missing={}, busy={}, invalid={}, failed={}",
        totals.scanned,
        totals.active,
        totals.orphaned,
        totals.deleted,
        totals.already_missing,
        totals.busy,
        totals.invalid,
        totals.failed,
    )];
    if !totals.affected_ids.is_empty() {
        lines.push(format!(
            "  affected: {}",
            bounded_project_id_summary(&totals.affected_ids)
        ));
    }
    lines
}

pub(super) fn optional_reconcile_totals_lines(
    label: &str,
    configured: bool,
    totals: Option<&ReconcileTotals>,
    orphan_buckets: Option<(usize, usize)>,
) -> Vec<String> {
    if !configured {
        return vec![format!("{label}: skipped (service not configured)")];
    }
    let Some(totals) = totals else {
        return vec![format!(
            "{label}: configured, but no reconciliation totals were produced"
        )];
    };

    let mut lines = reconcile_totals_lines(label, totals);
    if let Some((existing, pending_stale_project_cleanup)) = orphan_buckets {
        lines.push(format!(
            "  orphan buckets: existing={existing}, pending_stale_project_cleanup={pending_stale_project_cleanup}"
        ));
    }
    lines
}

pub(super) fn print_orphan_project_reconcile_totals(totals: &OrphanProjectReconcileTotals) {
    if totals.project_ids.is_empty() {
        return;
    }

    eprintln!(
        "Reconciled {} orphan code-index project(s): deleted {} SQL row(s) ({} file(s), {} symbol(s), {} content chunk(s), {} import(s), {} call(s)).",
        totals.project_ids.len(),
        totals.sql.total(),
        totals.sql.files_deleted,
        totals.sql.symbols_deleted,
        totals.sql.content_chunks_deleted,
        totals.sql.imports_deleted,
        totals.sql.calls_deleted
    );
    eprintln!(
        "  Project IDs: {}",
        bounded_project_id_summary(&totals.project_ids)
    );
    eprintln!(
        "  Cleared projections: {} graph project(s), {} vector collection(s); skipped {} graph, {} vector project(s).",
        totals.graph_projects_cleared,
        totals.vector_collections_deleted,
        totals.graph_projects_skipped,
        totals.vector_projects_skipped
    );
}

pub(super) fn bounded_project_id_summary(project_ids: &[String]) -> String {
    let mut ids = project_ids
        .iter()
        .take(ORPHAN_PROJECT_SUMMARY_LIMIT)
        .map(|id| short_id(id))
        .collect::<Vec<_>>();
    if project_ids.len() > ORPHAN_PROJECT_SUMMARY_LIMIT {
        ids.push(format!(
            "+{} more",
            project_ids.len() - ORPHAN_PROJECT_SUMMARY_LIMIT
        ));
    }
    ids.join(", ")
}

pub(super) fn warn_orphan_projection_cleanup_failure(
    store: &str,
    project_id: &str,
    error: anyhow::Error,
    warnings_emitted: &mut usize,
) {
    if *warnings_emitted < ORPHAN_PROJECT_WARNING_LIMIT {
        eprintln!(
            "Warning: {store} cleanup failed for orphan project {}: {error}",
            short_id(project_id)
        );
    } else if *warnings_emitted == ORPHAN_PROJECT_WARNING_LIMIT {
        eprintln!(
            "Warning: additional orphan project projection cleanup failures omitted after {ORPHAN_PROJECT_WARNING_LIMIT} warning(s)."
        );
    }
    *warnings_emitted += 1;
}

pub(super) fn print_current_project_projection_totals(totals: ProjectionPruneTotals) {
    if totals.graph_projects_cleaned > 0 {
        eprintln!(
            "Pruned graph projection: {} stale file(s), {} file-scoped node(s).",
            totals.graph_stale_files_deleted, totals.graph_nodes_deleted
        );
    } else if totals.graph_projects_skipped > 0 {
        eprintln!("Skipped graph projection orphan cleanup: FalkorDB is not configured.");
    }

    if totals.vector_projects_cleaned > 0 {
        eprintln!(
            "Pruned vector projection: {} stale file(s), {} vector point(s).",
            totals.vector_orphan_files_deleted, totals.vectors_deleted
        );
    } else if totals.vector_projects_skipped > 0 {
        eprintln!("Skipped vector projection orphan cleanup: Qdrant is not configured.");
    }
}

pub(super) fn print_all_project_projection_totals(totals: ProjectionPruneTotals) {
    if totals.graph_projects_cleaned > 0 {
        eprintln!(
            "Pruned graph projections for {} project(s): {} stale file(s), {} file-scoped node(s).",
            totals.graph_projects_cleaned,
            totals.graph_stale_files_deleted,
            totals.graph_nodes_deleted
        );
    } else if totals.graph_projects_skipped > 0 {
        eprintln!(
            "Skipped graph projection orphan cleanup for all indexed projects: FalkorDB is not configured."
        );
    }

    if totals.vector_projects_cleaned > 0 {
        eprintln!(
            "Pruned vector projections for {} project(s): {} stale file(s), {} vector point(s).",
            totals.vector_projects_cleaned,
            totals.vector_orphan_files_deleted,
            totals.vectors_deleted
        );
    } else if totals.vector_projects_skipped > 0 {
        eprintln!(
            "Skipped vector projection orphan cleanup for all indexed projects: Qdrant is not configured."
        );
    }
}

pub(super) fn warn_projection_cleanup_failure(
    store: &str,
    project_label: Option<&str>,
    error: anyhow::Error,
) {
    if let Some(project_label) = project_label {
        eprintln!("Warning: {store} projection orphan cleanup failed for {project_label}: {error}");
    } else {
        eprintln!("Warning: {store} projection orphan cleanup failed: {error}");
    }
}

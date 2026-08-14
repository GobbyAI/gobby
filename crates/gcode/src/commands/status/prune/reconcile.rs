#![allow(dead_code)]

use crate::utils::short_id;

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

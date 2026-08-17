use crate::cli_error::CliError;
use crate::config::{self, Context};
use crate::daemon::GlobalPruneOutcome;
use crate::index_lock::{IndexLockPolicy, lock_project_by_id};

use super::content_gc::{ContentGcCandidate, discover_content_gc, prune_content_versions};
use super::invalidate::invalidate_project_locked;
use super::projects::stale_projects;
use super::shared::{collect_projects_from, display_name};

mod inventory;
mod reconcile;

use reconcile::{ReconcileTotals, SweepOutcome, print_reconcile_totals, sweep_discovered_ids_with};

#[derive(Debug, Default, PartialEq, Eq)]
struct DestructiveSet {
    stale_project_ids: Vec<String>,
    orphan_collection_ids: Vec<String>,
    orphan_graph_scope_ids: Vec<String>,
    content_version_ids: Vec<String>,
}

impl DestructiveSet {
    fn is_empty(&self) -> bool {
        self.stale_project_ids.is_empty()
            && self.orphan_collection_ids.is_empty()
            && self.orphan_graph_scope_ids.is_empty()
            && self.content_version_ids.is_empty()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct StaleProjectPlan {
    id: String,
    label: String,
    reason: String,
}

struct GlobalPruneDiscovery {
    services: Context,
    machine_states: Vec<StaleProjectPlan>,
    content_gc_candidates: Vec<ContentGcCandidate>,
}

impl GlobalPruneDiscovery {
    fn destructive_set(&self) -> DestructiveSet {
        DestructiveSet {
            stale_project_ids: self
                .machine_states
                .iter()
                .map(|project| project.id.clone())
                .collect(),
            orphan_collection_ids: Vec::new(),
            orphan_graph_scope_ids: Vec::new(),
            content_version_ids: self
                .content_gc_candidates
                .iter()
                .map(|candidate| candidate.id.clone())
                .collect(),
        }
    }
}

fn authorize_prune_with(
    force: bool,
    pending: &DestructiveSet,
    confirm: impl FnOnce(&DestructiveSet) -> anyhow::Result<bool>,
) -> anyhow::Result<bool> {
    if force || pending.is_empty() {
        return Ok(true);
    }
    confirm(pending)
}

#[derive(Debug, PartialEq, Eq)]
enum ProjectionCleanupScope {
    AllIndexedProjects,
    ResolvedProjectOverride,
}

fn projection_cleanup_scope(project_override: Option<&str>) -> ProjectionCleanupScope {
    if project_override.is_some() {
        ProjectionCleanupScope::ResolvedProjectOverride
    } else {
        ProjectionCleanupScope::AllIndexedProjects
    }
}

pub fn prune(
    force: bool,
    project_override: Option<&str>,
    quiet: bool,
    retention_days: u32,
) -> anyhow::Result<()> {
    match projection_cleanup_scope(project_override) {
        ProjectionCleanupScope::AllIndexedProjects => prune_global(force, quiet, retention_days),
        ProjectionCleanupScope::ResolvedProjectOverride => {
            prune_project_scoped(force, project_override, quiet, retention_days)
        }
    }
}

fn prune_project_scoped(
    force: bool,
    project_override: Option<&str>,
    quiet: bool,
    retention_days: u32,
) -> anyhow::Result<()> {
    let ctx = Context::resolve_with_services(
        project_override,
        quiet,
        config::ServiceConfigSelection::projection_cleanup(),
    )?;
    let discovery = discover_project_scoped_records(&ctx, retention_days)?;
    let pending = discovery.destructive_set();
    if !authorize_prune_with(force, &pending, |_| confirm_global_prune(&discovery))? {
        eprintln!("Aborted.");
        return Ok(());
    }
    let stale_totals = mutate_project_scoped_stale(&discovery);
    print_reconcile_totals("Stale project reconciliation", &stale_totals);
    let content_gc_totals =
        prune_content_versions(&discovery.services, &discovery.content_gc_candidates)?;
    print_content_gc_totals(&content_gc_totals);
    if stale_totals.has_failures() || content_gc_totals.failed_versions > 0 {
        anyhow::bail!(
            "gcode prune completed with {} reconciliation failure(s)",
            stale_totals.failed + content_gc_totals.failed_versions
        );
    }
    Ok(())
}

fn prune_global(force: bool, quiet: bool, retention_days: u32) -> anyhow::Result<()> {
    prune_global_with(
        force,
        quiet,
        retention_days,
        crate::daemon::post_code_index_prune,
    )
}

fn prune_global_with(
    force: bool,
    quiet: bool,
    retention_days: u32,
    post: impl FnOnce(bool, u32) -> Result<GlobalPruneOutcome, CliError>,
) -> anyhow::Result<()> {
    let outcome = post(force, retention_days)?;
    if !quiet {
        eprintln!(
            "Global prune: completed={}, failed={}, skipped={}",
            outcome.completed.len(),
            outcome.failed.len(),
            outcome.skipped.len()
        );
    }
    if !outcome.failed.is_empty() {
        anyhow::bail!(
            "gcode prune completed with {} reconciliation failure(s)",
            outcome.failed.len()
        );
    }
    Ok(())
}

fn print_content_gc_totals(totals: &super::content_gc::ContentGcTotals) {
    eprintln!(
        "Content GC: {} version(s), {} symbol(s) deleted, {} busy project(s), {} failed version(s), {} skipped (store unconfigured)",
        totals.deleted_versions,
        totals.deleted_symbols,
        totals.busy_projects,
        totals.failed_versions,
        totals.skipped_versions,
    );
}

fn discover_project_scoped_records(
    ctx: &Context,
    retention_days: u32,
) -> anyhow::Result<GlobalPruneDiscovery> {
    let all_projects = collect_projects_from(&ctx.database_url)?;
    let stale_projects = stale_projects(&all_projects)
        .into_iter()
        .filter(|project| project.project.id == ctx.project_id)
        .map(|project| StaleProjectPlan {
            id: project.project.id.clone(),
            label: display_name(project.project),
            reason: project.reason,
        })
        .collect();
    let content_gc_candidates =
        discover_content_gc(&ctx.database_url, retention_days, Some(&ctx.project_id))?;
    Ok(GlobalPruneDiscovery {
        services: ctx.clone(),
        machine_states: stale_projects,
        content_gc_candidates,
    })
}

fn confirm_global_prune(discovery: &GlobalPruneDiscovery) -> anyhow::Result<bool> {
    eprintln!(
        "Pending gcode prune: {} stale machine state(s), {} expired content version(s).",
        discovery.machine_states.len(),
        discovery.content_gc_candidates.len(),
    );
    for project in &discovery.machine_states {
        eprintln!(
            "  machine state: {} ({}) — {}",
            project.label, project.id, project.reason
        );
    }
    for candidate in &discovery.content_gc_candidates {
        eprintln!(
            "  content: {}:{}@{}",
            candidate.project_id, candidate.file_path, candidate.content_hash
        );
    }

    eprint!("\nRemove all listed stale and orphaned data? [y/N] ");
    let _ = std::io::Write::flush(&mut std::io::stderr());
    let mut input = String::new();
    std::io::stdin().read_line(&mut input)?;
    Ok(input.trim().eq_ignore_ascii_case("y"))
}

fn mutate_project_scoped_stale(discovery: &GlobalPruneDiscovery) -> ReconcileTotals {
    let project_ids = discovery
        .machine_states
        .iter()
        .map(|project| project.id.clone())
        .collect::<Vec<_>>();
    sweep_discovered_ids_with(&project_ids, |project_id| {
        let Some(_lock) = lock_project_by_id(
            &discovery.services.database_url,
            project_id,
            IndexLockPolicy::maintenance_try(),
        )?
        else {
            return Ok(SweepOutcome::Busy);
        };
        let mut ctx = discovery.services.clone();
        ctx.project_id = project_id.to_string();
        invalidate_project_locked(&ctx)?;
        Ok(SweepOutcome::Deleted)
    })
}

#[cfg(test)]
#[path = "prune/tests.rs"]
mod tests;

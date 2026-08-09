use std::collections::HashSet;

use crate::config::{self, Context};
use crate::db;
use crate::graph::code_graph;
use crate::index_lock::{IndexLockPolicy, lock_project_by_id};
use crate::vector::code_symbols;

use super::content_gc::{ContentGcCandidate, discover_content_gc, prune_content_versions};
use super::invalidate::invalidate_project_locked;
use super::projects::stale_projects;
use super::shared::{collect_projects, display_name};

mod inventory;
mod reconcile;

use inventory::{
    CollectionInventory, ScopeInventory, all_collection_orphan_ids, all_scope_orphan_ids,
    classify_collection_inventory, classify_scope_inventory,
};
use reconcile::{
    ReconcileTotals, SweepOutcome, print_optional_reconcile_totals, print_reconcile_totals,
    sweep_discovered_ids_with,
};

const GLOBAL_SERVICE_CONTEXT_PROJECT_ID: &str = "00000000-0000-0000-0000-000000000000";

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
    stale_projects: Vec<StaleProjectPlan>,
    collections: Option<CollectionInventory>,
    graph_scopes: Option<ScopeInventory>,
    content_gc_candidates: Vec<ContentGcCandidate>,
}

struct GlobalProjectPruneDiscovery {
    authority: HashSet<String>,
    stale_project_ids: HashSet<String>,
    stale_projects: Vec<StaleProjectPlan>,
}

impl GlobalPruneDiscovery {
    fn destructive_set(&self) -> DestructiveSet {
        DestructiveSet {
            stale_project_ids: self
                .stale_projects
                .iter()
                .map(|project| project.id.clone())
                .collect(),
            orphan_collection_ids: self
                .collections
                .as_ref()
                .map(all_collection_orphan_ids)
                .unwrap_or_default(),
            orphan_graph_scope_ids: self
                .graph_scopes
                .as_ref()
                .map(all_scope_orphan_ids)
                .unwrap_or_default(),
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
    let stale_totals = mutate_stale_projects(&discovery);
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
    let mut discovery = discover_global_prune(quiet, retention_days)?;
    let pending = discovery.destructive_set();
    if !authorize_prune_with(force, &pending, |_| confirm_global_prune(&discovery))? {
        eprintln!("Aborted.");
        return Ok(());
    }

    let stale_totals = mutate_stale_projects(&discovery);
    print_reconcile_totals("Stale project reconciliation", &stale_totals);
    refresh_global_projection_inventories(&mut discovery, &pending)?;
    let collection_totals = mutate_orphan_collections(&discovery);
    let graph_totals = mutate_orphan_graph_scopes(&discovery);
    let content_gc_totals =
        prune_content_versions(&discovery.services, &discovery.content_gc_candidates)?;
    print_content_gc_totals(&content_gc_totals);
    print_optional_reconcile_totals(
        "Qdrant collection reconciliation",
        discovery.services.qdrant.is_some(),
        collection_totals.as_ref(),
        discovery.collections.as_ref().map(|inventory| {
            (
                inventory.existing_orphan_ids.len(),
                inventory.would_be_orphan_ids.len(),
            )
        }),
    );
    print_optional_reconcile_totals(
        "Falkor graph-scope reconciliation",
        discovery.services.falkordb.is_some(),
        graph_totals.as_ref(),
        discovery.graph_scopes.as_ref().map(|inventory| {
            (
                inventory.existing_orphan_ids.len(),
                inventory.would_be_orphan_ids.len(),
            )
        }),
    );
    let failed = stale_totals.failed
        + collection_totals.as_ref().map_or(0, |totals| totals.failed)
        + graph_totals.as_ref().map_or(0, |totals| totals.failed)
        + content_gc_totals.failed_versions;
    if stale_totals.has_failures()
        || collection_totals
            .as_ref()
            .is_some_and(ReconcileTotals::has_failures)
        || graph_totals
            .as_ref()
            .is_some_and(ReconcileTotals::has_failures)
        || content_gc_totals.failed_versions > 0
    {
        anyhow::bail!("gcode prune completed with {failed} reconciliation failure(s)");
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

fn discover_global_prune(quiet: bool, retention_days: u32) -> anyhow::Result<GlobalPruneDiscovery> {
    let all_projects = collect_projects()?;
    let database_url = db::resolve_database_url()?;
    let project_discovery = discover_global_project_prune(&all_projects);
    let content_gc_candidates = discover_content_gc(&database_url, retention_days, None)?;

    let services = Context::resolve_for_project_id_with_services(
        GLOBAL_SERVICE_CONTEXT_PROJECT_ID,
        quiet,
        config::ServiceConfigSelection::projection_cleanup(),
    )?;
    let (collections, graph_scopes) =
        discover_projection_inventories(&services, &project_discovery)?;

    Ok(GlobalPruneDiscovery {
        services,
        stale_projects: project_discovery.stale_projects,
        collections,
        graph_scopes,
        content_gc_candidates,
    })
}

fn discover_projection_inventories(
    services: &Context,
    project_discovery: &GlobalProjectPruneDiscovery,
) -> anyhow::Result<(Option<CollectionInventory>, Option<ScopeInventory>)> {
    let collections = services
        .qdrant
        .as_ref()
        .map(|qdrant| {
            code_symbols::list_code_symbol_collections(qdrant).map(|collections| {
                classify_collection_inventory(
                    &collections,
                    &project_discovery.authority,
                    &project_discovery.stale_project_ids,
                )
            })
        })
        .transpose()
        .map_err(anyhow::Error::from)?;
    let graph_scopes = services
        .falkordb
        .as_ref()
        .map(|falkor| {
            code_graph::list_project_scopes(falkor).map(|project_ids| {
                classify_scope_inventory(
                    &project_ids,
                    &project_discovery.authority,
                    &project_discovery.stale_project_ids,
                )
            })
        })
        .transpose()?;
    Ok((collections, graph_scopes))
}

fn refresh_global_projection_inventories(
    discovery: &mut GlobalPruneDiscovery,
    authorized: &DestructiveSet,
) -> anyhow::Result<()> {
    let projects = collect_projects()?;
    let project_discovery = discover_global_project_prune(&projects);
    let (collections, graph_scopes) =
        discover_projection_inventories(&discovery.services, &project_discovery)?;
    discovery.collections = collections.map(|mut inventory| {
        inventory
            .existing_orphan_ids
            .retain(|id| authorized.orphan_collection_ids.contains(id));
        inventory
            .would_be_orphan_ids
            .retain(|id| authorized.orphan_collection_ids.contains(id));
        inventory
    });
    discovery.graph_scopes = graph_scopes.map(|mut inventory| {
        inventory
            .existing_orphan_ids
            .retain(|id| authorized.orphan_graph_scope_ids.contains(id));
        inventory
            .would_be_orphan_ids
            .retain(|id| authorized.orphan_graph_scope_ids.contains(id));
        inventory
    });
    Ok(())
}

fn discover_global_project_prune(
    projects: &[crate::models::IndexedProject],
) -> GlobalProjectPruneDiscovery {
    // #17435/#17437 will add machine-owned root mappings. Until then, an absolute
    // root missing on this machine says nothing about shared project liveness.
    GlobalProjectPruneDiscovery {
        authority: projects.iter().map(|project| project.id.clone()).collect(),
        stale_project_ids: HashSet::new(),
        stale_projects: Vec::new(),
    }
}

fn discover_project_scoped_records(
    ctx: &Context,
    retention_days: u32,
) -> anyhow::Result<GlobalPruneDiscovery> {
    let all_projects = collect_projects()?;
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
        stale_projects,
        collections: None,
        graph_scopes: None,
        content_gc_candidates,
    })
}

fn confirm_global_prune(discovery: &GlobalPruneDiscovery) -> anyhow::Result<bool> {
    eprintln!(
        "Pending gcode prune: {} stale project(s), {} orphan collection(s), {} orphan graph scope(s), {} expired content version(s).",
        discovery.stale_projects.len(),
        discovery
            .collections
            .as_ref()
            .map(all_collection_orphan_ids)
            .map_or(0, |ids| ids.len()),
        discovery
            .graph_scopes
            .as_ref()
            .map(all_scope_orphan_ids)
            .map_or(0, |ids| ids.len()),
        discovery.content_gc_candidates.len(),
    );
    for project in &discovery.stale_projects {
        eprintln!(
            "  stale: {} ({}) — {}",
            project.label, project.id, project.reason
        );
    }
    if let Some(collections) = &discovery.collections {
        for project_id in all_collection_orphan_ids(collections) {
            eprintln!("  Qdrant: {project_id}");
        }
    }
    if let Some(scopes) = &discovery.graph_scopes {
        for project_id in all_scope_orphan_ids(scopes) {
            eprintln!("  Falkor: {project_id}");
        }
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

fn mutate_stale_projects(discovery: &GlobalPruneDiscovery) -> ReconcileTotals {
    let project_ids = discovery
        .stale_projects
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

fn mutate_orphan_collections(discovery: &GlobalPruneDiscovery) -> Option<ReconcileTotals> {
    let inventory = discovery.collections.as_ref()?;
    let mut totals = ReconcileTotals {
        scanned: inventory.scanned,
        active: inventory.active,
        orphaned: all_collection_orphan_ids(inventory).len(),
        invalid: inventory.invalid,
        ..ReconcileTotals::default()
    };
    let mutation = sweep_discovered_ids_with(&inventory.existing_orphan_ids, |project_id| {
        reconcile_orphan_collection(&discovery.services, project_id)
    });
    totals.merge_mutation(mutation);
    Some(totals)
}

fn reconcile_orphan_collection(ctx: &Context, project_id: &str) -> anyhow::Result<SweepOutcome> {
    let Some(_lock) = lock_project_by_id(
        &ctx.database_url,
        project_id,
        IndexLockPolicy::maintenance_try(),
    )?
    else {
        return Ok(SweepOutcome::Busy);
    };
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    if db::indexed_project_exists(&mut conn, project_id)? {
        return Ok(SweepOutcome::Active);
    }
    let qdrant = ctx
        .qdrant
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("Qdrant config disappeared after discovery"))?;
    match code_symbols::delete_project_collection(qdrant, project_id)? {
        0 => Ok(SweepOutcome::AlreadyMissing),
        _ => Ok(SweepOutcome::Deleted),
    }
}

fn mutate_orphan_graph_scopes(discovery: &GlobalPruneDiscovery) -> Option<ReconcileTotals> {
    let inventory = discovery.graph_scopes.as_ref()?;
    let mut totals = ReconcileTotals {
        scanned: inventory.scanned,
        active: inventory.active,
        orphaned: all_scope_orphan_ids(inventory).len(),
        invalid: inventory.invalid,
        ..ReconcileTotals::default()
    };
    let mutation = sweep_discovered_ids_with(&inventory.existing_orphan_ids, |project_id| {
        reconcile_orphan_graph_scope(&discovery.services, project_id)
    });
    totals.merge_mutation(mutation);
    Some(totals)
}

fn reconcile_orphan_graph_scope(ctx: &Context, project_id: &str) -> anyhow::Result<SweepOutcome> {
    let Some(_lock) = lock_project_by_id(
        &ctx.database_url,
        project_id,
        IndexLockPolicy::maintenance_try(),
    )?
    else {
        return Ok(SweepOutcome::Busy);
    };
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    if db::indexed_project_exists(&mut conn, project_id)? {
        return Ok(SweepOutcome::Active);
    }
    let mut project_ctx = ctx.clone();
    project_ctx.project_id = project_id.to_string();
    code_graph::clear_project(&project_ctx)?;
    Ok(SweepOutcome::Deleted)
}

#[cfg(test)]
#[path = "prune/tests.rs"]
mod tests;

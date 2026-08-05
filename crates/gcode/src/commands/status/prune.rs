use postgres::GenericClient;
use std::collections::HashSet;

use crate::config::{self, Context};
use crate::db;
use crate::graph::code_graph;
use crate::index_lock::{IndexLockPolicy, lock_project_by_id};
use crate::vector::code_symbols;

use super::invalidate::invalidate_project_locked;
use super::projects::stale_projects;
use super::shared::{collect_projects, display_name};

mod inventory;
mod reconcile;

use inventory::{
    CollectionInventory, ScopeInventory, all_collection_orphan_ids, all_scope_orphan_ids,
    classify_collection_inventory, classify_scope_inventory,
};
pub(super) use reconcile::OrphanSqlDeletionCounts;
use reconcile::{
    OrphanProjectReconcileTotals, ProjectionPruneTotals, ReconcileTotals, SweepOutcome,
    print_all_project_projection_totals, print_current_project_projection_totals,
    print_optional_reconcile_totals, print_orphan_project_reconcile_totals, print_reconcile_totals,
    sweep_discovered_ids_with, warn_orphan_projection_cleanup_failure,
    warn_projection_cleanup_failure,
};

const GLOBAL_SERVICE_CONTEXT_PROJECT_ID: &str = "00000000-0000-0000-0000-000000000000";

#[derive(Debug, Default, PartialEq, Eq)]
struct DestructiveSet {
    stale_project_ids: Vec<String>,
    orphan_collection_ids: Vec<String>,
    orphan_graph_scope_ids: Vec<String>,
    orphan_sql_project_ids: Vec<String>,
}

impl DestructiveSet {
    fn is_empty(&self) -> bool {
        self.stale_project_ids.is_empty()
            && self.orphan_collection_ids.is_empty()
            && self.orphan_graph_scope_ids.is_empty()
            && self.orphan_sql_project_ids.is_empty()
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
    orphan_sql_project_ids: Vec<String>,
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
            orphan_sql_project_ids: self.orphan_sql_project_ids.clone(),
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

pub fn prune(force: bool, project_override: Option<&str>, quiet: bool) -> anyhow::Result<()> {
    match projection_cleanup_scope(project_override) {
        ProjectionCleanupScope::AllIndexedProjects => prune_global(force, quiet),
        ProjectionCleanupScope::ResolvedProjectOverride => {
            prune_project_scoped(force, project_override, quiet)
        }
    }
}

fn prune_project_scoped(
    force: bool,
    project_override: Option<&str>,
    quiet: bool,
) -> anyhow::Result<()> {
    let ctx = Context::resolve_with_services(
        project_override,
        quiet,
        config::ServiceConfigSelection::projection_cleanup(),
    )?;
    let discovery = discover_project_scoped_records(&ctx)?;
    let pending = discovery.destructive_set();
    if !authorize_prune_with(force, &pending, |_| confirm_global_prune(&discovery))? {
        eprintln!("Aborted.");
        return Ok(());
    }
    let stale_totals = mutate_stale_projects(&discovery);
    let orphan_totals = reconcile_orphan_projects(discovery.orphan_sql_project_ids.clone(), quiet)?;
    print_reconcile_totals("Stale project reconciliation", &stale_totals);
    print_orphan_project_reconcile_totals(&orphan_totals);
    if stale_totals.has_failures() {
        anyhow::bail!(
            "gcode prune completed with {} reconciliation failure(s)",
            stale_totals.failed
        );
    }
    prune_current_project_projections(&ctx)
}

fn prune_global(force: bool, quiet: bool) -> anyhow::Result<()> {
    let discovery = discover_global_prune(quiet)?;
    let pending = discovery.destructive_set();
    if !authorize_prune_with(force, &pending, |_| confirm_global_prune(&discovery))? {
        eprintln!("Aborted.");
        return Ok(());
    }

    let stale_totals = mutate_stale_projects(&discovery);
    let collection_totals = mutate_orphan_collections(&discovery);
    let graph_totals = mutate_orphan_graph_scopes(&discovery);
    let orphan_totals = reconcile_orphan_projects(discovery.orphan_sql_project_ids.clone(), quiet)?;

    print_reconcile_totals("Stale project reconciliation", &stale_totals);
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
    print_orphan_project_reconcile_totals(&orphan_totals);
    prune_all_project_projections(quiet)?;

    let failed = stale_totals.failed
        + collection_totals.as_ref().map_or(0, |totals| totals.failed)
        + graph_totals.as_ref().map_or(0, |totals| totals.failed);
    if stale_totals.has_failures()
        || collection_totals
            .as_ref()
            .is_some_and(ReconcileTotals::has_failures)
        || graph_totals
            .as_ref()
            .is_some_and(ReconcileTotals::has_failures)
    {
        anyhow::bail!("gcode prune completed with {failed} reconciliation failure(s)");
    }
    Ok(())
}

fn discover_global_prune(quiet: bool) -> anyhow::Result<GlobalPruneDiscovery> {
    let all_projects = collect_projects()?;
    let database_url = db::resolve_database_url()?;
    let mut conn = db::connect_readonly(&database_url)?;
    let orphan_sql_project_ids = collect_orphan_project_ids(&mut conn)?;
    let authority = global_project_authority(&all_projects);
    // #17435/#17437 will add machine-owned root mappings. Until then, an absolute
    // root missing on this machine says nothing about shared project liveness.
    let stale_project_ids = HashSet::new();
    let stale_projects = Vec::new();

    let services = Context::resolve_for_project_id_with_services(
        GLOBAL_SERVICE_CONTEXT_PROJECT_ID,
        quiet,
        config::ServiceConfigSelection::projection_cleanup(),
    )?;
    let collections = services
        .qdrant
        .as_ref()
        .map(|qdrant| {
            code_symbols::list_code_symbol_collections(qdrant).map(|collections| {
                classify_collection_inventory(&collections, &authority, &stale_project_ids)
            })
        })
        .transpose()
        .map_err(anyhow::Error::from)?;
    let graph_scopes = services
        .falkordb
        .as_ref()
        .map(|falkor| {
            code_graph::list_project_scopes(falkor).map(|project_ids| {
                classify_scope_inventory(&project_ids, &authority, &stale_project_ids)
            })
        })
        .transpose()?;

    Ok(GlobalPruneDiscovery {
        services,
        stale_projects,
        collections,
        graph_scopes,
        orphan_sql_project_ids,
    })
}

fn global_project_authority(projects: &[crate::models::IndexedProject]) -> HashSet<String> {
    projects.iter().map(|project| project.id.clone()).collect()
}

fn discover_project_scoped_records(ctx: &Context) -> anyhow::Result<GlobalPruneDiscovery> {
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
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let orphan_sql_project_ids = collect_orphan_project_ids(&mut conn)?
        .into_iter()
        .filter(|project_id| project_id == &ctx.project_id)
        .collect();
    Ok(GlobalPruneDiscovery {
        services: ctx.clone(),
        stale_projects,
        collections: None,
        graph_scopes: None,
        orphan_sql_project_ids,
    })
}

fn confirm_global_prune(discovery: &GlobalPruneDiscovery) -> anyhow::Result<bool> {
    eprintln!(
        "Pending gcode prune: {} stale project(s), {} orphan collection(s), {} orphan graph scope(s), {} orphan SQL project scope(s).",
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
        discovery.orphan_sql_project_ids.len(),
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
    for project_id in &discovery.orphan_sql_project_ids {
        eprintln!("  SQL orphan rows: {project_id}");
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

fn reconcile_orphan_projects(
    project_ids: Vec<String>,
    quiet: bool,
) -> anyhow::Result<OrphanProjectReconcileTotals> {
    let database_url = db::resolve_database_url()?;
    let mut conn = db::connect_readwrite(&database_url)?;
    let mut totals = OrphanProjectReconcileTotals::default();
    let mut warnings_emitted = 0usize;

    for project_id in project_ids {
        if cleanup_orphan_project_projections(
            &project_id,
            quiet,
            &mut totals,
            &mut warnings_emitted,
        ) {
            let counts = delete_orphan_project_sql_rows(&mut conn, &project_id)?;
            totals.record_sql(project_id, counts);
        }
    }

    Ok(totals)
}

pub(super) fn collect_orphan_project_ids(
    conn: &mut impl GenericClient,
) -> anyhow::Result<Vec<String>> {
    let rows = conn.query(
        "SELECT child.project_id
         FROM (
             SELECT project_id FROM code_indexed_files
             UNION
             SELECT project_id FROM code_symbols
             UNION
             SELECT project_id FROM code_content_chunks
             UNION
             SELECT project_id FROM code_imports
             UNION
             SELECT project_id FROM code_calls
         ) child
         LEFT JOIN code_indexed_projects parent ON parent.id = child.project_id
         WHERE parent.id IS NULL
         ORDER BY child.project_id",
        &[],
    )?;

    rows.into_iter().map(|row| db::id_string(&row, 0)).collect()
}

pub(super) fn delete_orphan_project_sql_rows(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<OrphanSqlDeletionCounts> {
    let project_id = db::id_param(project_id)?;
    let calls_deleted = conn.execute(
        "DELETE FROM code_calls WHERE project_id = $1",
        &[&project_id],
    )?;
    let imports_deleted = conn.execute(
        "DELETE FROM code_imports WHERE project_id = $1",
        &[&project_id],
    )?;
    let content_chunks_deleted = conn.execute(
        "DELETE FROM code_content_chunks WHERE project_id = $1",
        &[&project_id],
    )?;
    let files_deleted = conn.execute(
        "DELETE FROM code_indexed_files WHERE project_id = $1",
        &[&project_id],
    )?;
    let symbols_deleted = conn.execute(
        "DELETE FROM code_symbols WHERE project_id = $1",
        &[&project_id],
    )?;

    Ok(OrphanSqlDeletionCounts {
        symbols_deleted,
        files_deleted,
        content_chunks_deleted,
        imports_deleted,
        calls_deleted,
    })
}

fn cleanup_orphan_project_projections(
    project_id: &str,
    quiet: bool,
    totals: &mut OrphanProjectReconcileTotals,
    warnings_emitted: &mut usize,
) -> bool {
    let mut cleaned = true;
    let mut skipped = false;
    let ctx = match Context::resolve_for_project_id_with_services(
        project_id,
        quiet,
        config::ServiceConfigSelection::projection_cleanup(),
    ) {
        Ok(ctx) => ctx,
        Err(error) => {
            warn_orphan_projection_cleanup_failure(
                "service config",
                project_id,
                error,
                warnings_emitted,
            );
            totals.graph_projects_skipped += 1;
            totals.vector_projects_skipped += 1;
            return false;
        }
    };

    if ctx.falkordb.is_some() {
        if let Err(error) = code_graph::clear_project(&ctx) {
            warn_orphan_projection_cleanup_failure("graph", project_id, error, warnings_emitted);
            cleaned = false;
        } else {
            totals.graph_projects_cleared += 1;
        }
    } else {
        totals.graph_projects_skipped += 1;
        skipped = true;
    }

    if let Some(qdrant) = &ctx.qdrant {
        match code_symbols::delete_project_collection(qdrant, project_id) {
            Ok(deleted) => totals.vector_collections_deleted += deleted,
            Err(error) => {
                warn_orphan_projection_cleanup_failure(
                    "vector",
                    project_id,
                    anyhow::Error::from(error),
                    warnings_emitted,
                );
                cleaned = false;
            }
        }
    } else {
        totals.vector_projects_skipped += 1;
        skipped = true;
    }
    orphan_projection_cleanup_confirmed(cleaned, skipped)
}

fn orphan_projection_cleanup_confirmed(cleaned: bool, skipped: bool) -> bool {
    // Keep SQL discovery rows when any projection store was skipped; they are
    // what lets a later prune with full service config find and clear orphans.
    cleaned && !skipped
}

fn prune_all_project_projections(quiet: bool) -> anyhow::Result<()> {
    let projects = collect_projects()?;
    if projects.is_empty() {
        eprintln!("No indexed projects remain for projection cleanup.");
        return Ok(());
    }

    let mut totals = ProjectionPruneTotals::default();
    for project in &projects {
        let label = display_name(project);
        match Context::resolve_for_project_id_with_services(
            &project.id,
            quiet,
            config::ServiceConfigSelection::projection_cleanup(),
        ) {
            Ok(ctx) => totals.add(prune_project_orphan_projections(&ctx, Some(&label))),
            Err(error) => {
                eprintln!("Warning: projection orphan cleanup failed for {label}: {error}")
            }
        }
    }

    print_all_project_projection_totals(totals);
    Ok(())
}

fn prune_current_project_projections(ctx: &Context) -> anyhow::Result<()> {
    let totals = prune_project_orphan_projections(ctx, None);
    print_current_project_projection_totals(totals);
    Ok(())
}

fn prune_project_orphan_projections(
    ctx: &Context,
    project_label: Option<&str>,
) -> ProjectionPruneTotals {
    let mut totals = ProjectionPruneTotals::default();

    match prune_graph_orphans(ctx) {
        Ok(Some(cleanup)) => totals.record_graph_cleanup(cleanup),
        Ok(None) => totals.graph_projects_skipped += 1,
        Err(error) => warn_projection_cleanup_failure("graph", project_label, error),
    }

    match prune_vector_orphans(ctx) {
        Ok(Some(cleanup)) => totals.record_vector_cleanup(cleanup),
        Ok(None) => totals.vector_projects_skipped += 1,
        Err(error) => warn_projection_cleanup_failure("vector", project_label, error),
    }

    totals
}

fn prune_graph_orphans(
    ctx: &Context,
) -> anyhow::Result<Option<crate::graph::code_graph::GraphOrphanCleanup>> {
    if ctx.falkordb.is_none() {
        return Ok(None);
    }
    crate::commands::graph::cleanup_deleted_project_graph(ctx).map(Some)
}

fn prune_vector_orphans(
    ctx: &Context,
) -> anyhow::Result<Option<code_symbols::VectorOrphanCleanup>> {
    let Some(qdrant) = &ctx.qdrant else {
        return Ok(None);
    };
    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let indexed_file_paths = db::list_indexed_file_paths(&mut conn, &ctx.project_id)?
        .into_iter()
        .collect::<HashSet<_>>();
    code_symbols::cleanup_orphan_file_vectors(qdrant, &ctx.project_id, &indexed_file_paths)
        .map(Some)
        .map_err(anyhow::Error::from)
}

#[cfg(test)]
#[path = "prune/tests.rs"]
mod tests;

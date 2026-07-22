use postgres::GenericClient;
use std::collections::HashSet;

use crate::config::{self, Context};
use crate::db;
use crate::graph::code_graph;
use crate::index_lock::{IndexLockPolicy, lock_project_by_id};
use crate::utils::short_id;
use crate::vector::code_symbols;

use super::invalidate::invalidate_project_locked;
use super::projects::stale_projects;
use super::shared::{collect_projects, display_name};

const ORPHAN_PROJECT_WARNING_LIMIT: usize = 5;
const ORPHAN_PROJECT_SUMMARY_LIMIT: usize = 8;
const GLOBAL_SERVICE_CONTEXT_PROJECT_ID: &str = "00000000-0000-0000-0000-000000000000";

#[derive(Debug, Default, PartialEq, Eq)]
struct CollectionInventory {
    scanned: usize,
    active: usize,
    invalid: usize,
    existing_orphan_ids: Vec<String>,
    would_be_orphan_ids: Vec<String>,
}

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

#[derive(Debug, Default, PartialEq, Eq)]
struct ScopeInventory {
    scanned: usize,
    active: usize,
    invalid: usize,
    existing_orphan_ids: Vec<String>,
    would_be_orphan_ids: Vec<String>,
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

fn classify_collection_inventory(
    collections: &[String],
    authority: &HashSet<String>,
    stale_project_ids: &HashSet<String>,
) -> CollectionInventory {
    let mut inventory = CollectionInventory::default();
    for collection in collections {
        let Some(project_id) = collection.strip_prefix(config::CODE_SYMBOL_COLLECTION_PREFIX)
        else {
            continue;
        };
        inventory.scanned += 1;

        let Ok(parsed) = uuid::Uuid::parse_str(project_id) else {
            inventory.invalid += 1;
            continue;
        };
        if parsed.to_string() != project_id {
            inventory.invalid += 1;
            continue;
        }

        if stale_project_ids.contains(project_id) {
            inventory.would_be_orphan_ids.push(project_id.to_string());
        } else if authority.contains(project_id) {
            inventory.active += 1;
        } else {
            inventory.existing_orphan_ids.push(project_id.to_string());
        }
    }
    inventory
}

fn classify_scope_inventory(
    project_ids: &[String],
    authority: &HashSet<String>,
    stale_project_ids: &HashSet<String>,
) -> ScopeInventory {
    let mut inventory = ScopeInventory::default();
    for project_id in project_ids {
        inventory.scanned += 1;
        let Ok(parsed) = uuid::Uuid::parse_str(project_id) else {
            inventory.invalid += 1;
            continue;
        };
        if parsed.to_string() != *project_id {
            inventory.invalid += 1;
            continue;
        }
        if stale_project_ids.contains(project_id) {
            inventory.would_be_orphan_ids.push(project_id.clone());
        } else if authority.contains(project_id) {
            inventory.active += 1;
        } else {
            inventory.existing_orphan_ids.push(project_id.clone());
        }
    }
    inventory
}

fn all_collection_orphan_ids(inventory: &CollectionInventory) -> Vec<String> {
    sorted_union(
        &inventory.existing_orphan_ids,
        &inventory.would_be_orphan_ids,
    )
}

fn all_scope_orphan_ids(inventory: &ScopeInventory) -> Vec<String> {
    sorted_union(
        &inventory.existing_orphan_ids,
        &inventory.would_be_orphan_ids,
    )
}

fn sorted_union(left: &[String], right: &[String]) -> Vec<String> {
    left.iter()
        .chain(right)
        .cloned()
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .collect()
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SweepOutcome {
    Active,
    Deleted,
    AlreadyMissing,
    Busy,
}

#[derive(Debug, Default, PartialEq, Eq)]
struct ReconcileTotals {
    scanned: usize,
    active: usize,
    orphaned: usize,
    deleted: usize,
    already_missing: usize,
    busy: usize,
    invalid: usize,
    failed: usize,
    affected_ids: Vec<String>,
}

impl ReconcileTotals {
    fn has_failures(&self) -> bool {
        self.failed > 0
    }

    fn merge_mutation(&mut self, mutation: ReconcileTotals) {
        self.active += mutation.active;
        self.deleted += mutation.deleted;
        self.already_missing += mutation.already_missing;
        self.busy += mutation.busy;
        self.failed += mutation.failed;
        self.affected_ids.extend(mutation.affected_ids);
    }
}

fn sweep_discovered_ids_with(
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

#[derive(Debug, PartialEq, Eq)]
enum ProjectionCleanupScope {
    AllIndexedProjects,
    ResolvedProjectOverride,
}

#[derive(Default)]
struct ProjectionPruneTotals {
    graph_projects_cleaned: usize,
    graph_projects_skipped: usize,
    graph_stale_files_deleted: usize,
    graph_nodes_deleted: usize,
    vector_projects_cleaned: usize,
    vector_projects_skipped: usize,
    vector_orphan_files_deleted: usize,
    vectors_deleted: usize,
}

impl ProjectionPruneTotals {
    fn record_graph_cleanup(&mut self, cleanup: crate::graph::code_graph::GraphOrphanCleanup) {
        self.graph_projects_cleaned += 1;
        self.graph_stale_files_deleted += cleanup.stale_files_deleted;
        self.graph_nodes_deleted += cleanup.graph_nodes_deleted;
    }

    fn record_vector_cleanup(&mut self, cleanup: code_symbols::VectorOrphanCleanup) {
        self.vector_projects_cleaned += 1;
        self.vector_orphan_files_deleted += cleanup.orphan_files_deleted;
        self.vectors_deleted += cleanup.vectors_deleted;
    }

    fn add(&mut self, other: ProjectionPruneTotals) {
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
pub(super) struct OrphanSqlDeletionCounts {
    symbols_deleted: u64,
    files_deleted: u64,
    content_chunks_deleted: u64,
    imports_deleted: u64,
    calls_deleted: u64,
}

impl OrphanSqlDeletionCounts {
    fn total(&self) -> u64 {
        self.symbols_deleted
            + self.files_deleted
            + self.content_chunks_deleted
            + self.imports_deleted
            + self.calls_deleted
    }
}

#[derive(Default)]
struct OrphanProjectReconcileTotals {
    project_ids: Vec<String>,
    sql: OrphanSqlDeletionCounts,
    graph_projects_cleared: usize,
    graph_projects_skipped: usize,
    vector_collections_deleted: usize,
    vector_projects_skipped: usize,
}

impl OrphanProjectReconcileTotals {
    fn record_sql(&mut self, project_id: String, counts: OrphanSqlDeletionCounts) {
        self.project_ids.push(project_id);
        self.sql.symbols_deleted += counts.symbols_deleted;
        self.sql.files_deleted += counts.files_deleted;
        self.sql.content_chunks_deleted += counts.content_chunks_deleted;
        self.sql.imports_deleted += counts.imports_deleted;
        self.sql.calls_deleted += counts.calls_deleted;
    }
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
    let discovery = discover_project_scoped_records(quiet)?;
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
    prune_resolved_project_projections(project_override, quiet)
}

fn prune_resolved_project_projections(
    project_override: Option<&str>,
    quiet: bool,
) -> anyhow::Result<()> {
    match Context::resolve_with_services(
        project_override,
        quiet,
        config::ServiceConfigSelection::projection_cleanup(),
    ) {
        Ok(ctx) => prune_current_project_projections(&ctx),
        Err(error) if project_override.is_none() && is_missing_project_context(&error) => Ok(()),
        Err(error) => Err(error),
    }
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
        discovery.collections.is_some(),
        collection_totals.as_ref(),
    );
    print_optional_reconcile_totals(
        "Falkor graph-scope reconciliation",
        discovery.graph_scopes.is_some(),
        graph_totals.as_ref(),
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
    let stale = stale_projects(&all_projects);
    let database_url = db::resolve_database_url()?;
    let mut conn = db::connect_readonly(&database_url)?;
    let orphan_sql_project_ids = collect_orphan_project_ids(&mut conn)?;
    let authority = all_projects
        .iter()
        .map(|project| project.id.clone())
        .collect::<HashSet<_>>();
    let stale_project_ids = stale
        .iter()
        .map(|project| project.project.id.clone())
        .collect::<HashSet<_>>();
    let stale_projects = stale
        .into_iter()
        .map(|project| StaleProjectPlan {
            id: project.project.id.clone(),
            label: display_name(project.project),
            reason: project.reason,
        })
        .collect();

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

fn discover_project_scoped_records(quiet: bool) -> anyhow::Result<GlobalPruneDiscovery> {
    let all_projects = collect_projects()?;
    let stale_projects = stale_projects(&all_projects)
        .into_iter()
        .map(|project| StaleProjectPlan {
            id: project.project.id.clone(),
            label: display_name(project.project),
            reason: project.reason,
        })
        .collect();
    let database_url = db::resolve_database_url()?;
    let mut conn = db::connect_readonly(&database_url)?;
    let orphan_sql_project_ids = collect_orphan_project_ids(&mut conn)?;
    let services = Context::resolve_for_project_id_with_services(
        GLOBAL_SERVICE_CONTEXT_PROJECT_ID,
        quiet,
        config::ServiceConfigSelection::projection_cleanup(),
    )?;
    Ok(GlobalPruneDiscovery {
        services,
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

fn print_reconcile_totals(label: &str, totals: &ReconcileTotals) {
    eprintln!(
        "{label}: scanned={}, active={}, orphaned={}, deleted={}, already_missing={}, busy={}, invalid={}, failed={}",
        totals.scanned,
        totals.active,
        totals.orphaned,
        totals.deleted,
        totals.already_missing,
        totals.busy,
        totals.invalid,
        totals.failed,
    );
    if !totals.affected_ids.is_empty() {
        eprintln!(
            "  affected: {}",
            bounded_project_id_summary(&totals.affected_ids)
        );
    }
}

fn print_optional_reconcile_totals(
    label: &str,
    configured: bool,
    totals: Option<&ReconcileTotals>,
) {
    if configured {
        if let Some(totals) = totals {
            print_reconcile_totals(label, totals);
        }
    } else {
        eprintln!("{label}: skipped (service not configured)");
    }
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

fn print_orphan_project_reconcile_totals(totals: &OrphanProjectReconcileTotals) {
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

fn bounded_project_id_summary(project_ids: &[String]) -> String {
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

fn warn_orphan_projection_cleanup_failure(
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

fn print_current_project_projection_totals(totals: ProjectionPruneTotals) {
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

fn print_all_project_projection_totals(totals: ProjectionPruneTotals) {
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

fn warn_projection_cleanup_failure(store: &str, project_label: Option<&str>, error: anyhow::Error) {
    if let Some(project_label) = project_label {
        eprintln!("Warning: {store} projection orphan cleanup failed for {project_label}: {error}");
    } else {
        eprintln!("Warning: {store} projection orphan cleanup failed: {error}");
    }
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

fn is_missing_project_context(error: &anyhow::Error) -> bool {
    error
        .to_string()
        .contains("No gcode project found. Run `gcode init`")
}

#[cfg(test)]
mod tests {
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::*;

    #[test]
    fn prune_without_project_uses_all_indexed_projection_scope() {
        assert_eq!(
            projection_cleanup_scope(None),
            ProjectionCleanupScope::AllIndexedProjects
        );
    }

    #[test]
    fn prune_with_project_uses_single_resolved_projection_scope() {
        assert_eq!(
            projection_cleanup_scope(Some("/tmp/project")),
            ProjectionCleanupScope::ResolvedProjectOverride
        );
    }

    #[test]
    fn orphan_projection_cleanup_requires_confirmed_non_skipped_cleanup() {
        assert!(orphan_projection_cleanup_confirmed(true, false));
        assert!(!orphan_projection_cleanup_confirmed(true, true));
        assert!(!orphan_projection_cleanup_confirmed(false, false));
        assert!(!orphan_projection_cleanup_confirmed(false, true));
    }

    #[test]
    fn bounded_project_id_summary_caps_ids() {
        let ids = (0..10)
            .map(|idx| format!("project-{idx:02}-abcdef"))
            .collect::<Vec<_>>();

        let summary = bounded_project_id_summary(&ids);

        assert!(summary.contains("project-"));
        assert!(summary.contains("+2 more"));
    }

    #[test]
    fn global_prune_strict_collection_classification() {
        let active_id = "11111111-1111-1111-1111-111111111111";
        let stale_id = "22222222-2222-2222-2222-222222222222";
        let orphan_id = "33333333-3333-3333-3333-333333333333";
        let authority = HashSet::from([active_id.to_string(), stale_id.to_string()]);
        let stale = HashSet::from([stale_id.to_string()]);
        let collections = vec![
            format!("code_symbols_{active_id}"),
            format!("code_symbols_{stale_id}"),
            format!("code_symbols_{orphan_id}"),
            "code_symbols_33333333333333333333333333333333".to_string(),
            "code_symbols_44444444-4444-4444-4444-44444444444A".to_string(),
            "code_symbols_invalid".to_string(),
            "memory_vectors".to_string(),
        ];

        let inventory = classify_collection_inventory(&collections, &authority, &stale);

        assert_eq!(inventory.scanned, 6);
        assert_eq!(inventory.active, 1);
        assert_eq!(inventory.invalid, 3);
        assert_eq!(inventory.existing_orphan_ids, vec![orphan_id.to_string()]);
        assert_eq!(inventory.would_be_orphan_ids, vec![stale_id.to_string()]);
    }

    #[test]
    fn global_prune_authorization_matrix_uses_single_gate() {
        let cases = [
            DestructiveSet {
                stale_project_ids: vec!["stale".to_string()],
                orphan_collection_ids: vec!["collection".to_string()],
                orphan_graph_scope_ids: Vec::new(),
                orphan_sql_project_ids: Vec::new(),
            },
            DestructiveSet {
                stale_project_ids: vec!["stale".to_string()],
                ..DestructiveSet::default()
            },
            DestructiveSet {
                orphan_collection_ids: vec!["collection".to_string()],
                ..DestructiveSet::default()
            },
            DestructiveSet {
                orphan_graph_scope_ids: vec!["graph".to_string()],
                ..DestructiveSet::default()
            },
        ];

        for pending in cases {
            let mut prompts = 0;
            let authorized = authorize_prune_with(false, &pending, |_| {
                prompts += 1;
                Ok(true)
            })
            .expect("authorize prune");
            assert!(authorized);
            assert_eq!(prompts, 1);
        }

        let pending = DestructiveSet {
            stale_project_ids: vec!["stale".to_string()],
            orphan_collection_ids: vec!["collection".to_string()],
            orphan_graph_scope_ids: vec!["graph".to_string()],
            orphan_sql_project_ids: Vec::new(),
        };
        let mut prompts = 0;
        let authorized = authorize_prune_with(false, &pending, |_| {
            prompts += 1;
            Ok(false)
        })
        .expect("decline prune");
        assert!(!authorized);
        assert_eq!(prompts, 1);

        let mut forced_prompts = 0;
        assert!(
            authorize_prune_with(true, &pending, |_| {
                forced_prompts += 1;
                Ok(false)
            })
            .expect("force prune")
        );
        assert_eq!(forced_prompts, 0);
    }

    #[test]
    fn global_prune_sweep_rechecks_and_continues_after_failures() {
        let ids = vec![
            "active".to_string(),
            "busy".to_string(),
            "deleted".to_string(),
            "missing".to_string(),
            "failed".to_string(),
        ];
        let mut visited = Vec::new();

        let totals = sweep_discovered_ids_with(&ids, |project_id| {
            visited.push(project_id.to_string());
            match project_id {
                "active" => Ok(SweepOutcome::Active),
                "busy" => Ok(SweepOutcome::Busy),
                "deleted" => Ok(SweepOutcome::Deleted),
                "missing" => Ok(SweepOutcome::AlreadyMissing),
                "failed" => anyhow::bail!("backend delete failed"),
                _ => unreachable!(),
            }
        });

        assert_eq!(visited, ids);
        assert_eq!(totals.scanned, 5);
        assert_eq!(totals.active, 1);
        assert_eq!(totals.orphaned, 5);
        assert_eq!(totals.deleted, 1);
        assert_eq!(totals.already_missing, 1);
        assert_eq!(totals.busy, 1);
        assert_eq!(totals.failed, 1);
        assert!(totals.has_failures());
    }

    #[test]
    fn global_prune_collection_recheck_retains_row_inserted_after_discovery() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-prune-recheck");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        let authority = HashSet::new();
        let inventory = classify_collection_inventory(
            &[format!("code_symbols_{project_id}")],
            &authority,
            &HashSet::new(),
        );
        assert_eq!(inventory.existing_orphan_ids, vec![project_id.clone()]);

        seed_project_with_child_rows(&mut conn, &project_id, true);
        let ctx = prune_test_context(database_url, &project_id, true);

        assert_eq!(
            reconcile_orphan_collection(&ctx, &project_id).expect("recheck collection"),
            SweepOutcome::Active
        );
        assert!(db::indexed_project_exists(&mut conn, &project_id).expect("project exists"));
    }

    #[test]
    fn global_prune_busy_lock_defers_entire_stale_project() {
        let (mut conn, database_url) = connect_test_db();
        let project_id = unique_test_project_id("gcode-prune-stale-busy");
        cleanup_project(&mut conn, &project_id).expect("pre-clean project rows");
        let _cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: project_id.clone(),
        };
        seed_project_with_child_rows(&mut conn, &project_id, true);
        let lock_key = crate::index_lock::project_lock_key(&project_id);
        conn.query_one("SELECT pg_advisory_lock($1)", &[&lock_key])
            .expect("hold project lock");

        let discovery = GlobalPruneDiscovery {
            services: prune_test_context(database_url, &project_id, true),
            stale_projects: vec![StaleProjectPlan {
                id: project_id.clone(),
                label: project_id.clone(),
                reason: "test stale project".to_string(),
            }],
            collections: None,
            graph_scopes: None,
            orphan_sql_project_ids: Vec::new(),
        };
        let totals = mutate_stale_projects(&discovery);

        conn.query_one("SELECT pg_advisory_unlock($1)", &[&lock_key])
            .expect("release project lock");
        assert_eq!(totals.busy, 1);
        assert_eq!(totals.deleted, 0);
        assert_eq!(totals.failed, 0);
        assert!(db::indexed_project_exists(&mut conn, &project_id).expect("project exists"));
        assert_eq!(project_child_row_count(&mut conn, &project_id), 5);
    }

    #[test]
    #[cfg_attr(
        not(gcode_postgres_tests),
        ignore = "requires a PostgreSQL test database URL"
    )]
    #[serial_test::serial(serial_db)]
    fn orphan_project_discovery_and_sql_deletion_counts() {
        let (mut conn, database_url) = connect_test_db();
        let valid_project_id = unique_test_project_id("gcode-orphan-valid");
        let orphan_project_id = unique_test_project_id("gcode-orphan-missing-parent");
        cleanup_project(&mut conn, &valid_project_id).expect("pre-clean valid project rows");
        cleanup_project(&mut conn, &orphan_project_id).expect("pre-clean orphan project rows");
        let _valid_cleanup = ProjectCleanup {
            database_url: database_url.clone(),
            project_id: valid_project_id.clone(),
        };
        let _orphan_cleanup = ProjectCleanup {
            database_url,
            project_id: orphan_project_id.clone(),
        };

        seed_project_with_child_rows(&mut conn, &valid_project_id, true);
        seed_project_with_child_rows(&mut conn, &orphan_project_id, false);

        let orphan_ids = collect_orphan_project_ids(&mut conn).expect("discover orphan projects");
        assert!(orphan_ids.contains(&orphan_project_id));
        assert!(!orphan_ids.contains(&valid_project_id));

        let counts = delete_orphan_project_sql_rows(&mut conn, &orphan_project_id)
            .expect("delete orphan rows");

        assert_eq!(
            counts,
            OrphanSqlDeletionCounts {
                symbols_deleted: 1,
                files_deleted: 1,
                content_chunks_deleted: 1,
                imports_deleted: 1,
                calls_deleted: 1,
            }
        );
        assert_eq!(project_child_row_count(&mut conn, &orphan_project_id), 0);
        assert_eq!(project_child_row_count(&mut conn, &valid_project_id), 5);
    }

    struct ProjectCleanup {
        database_url: String,
        project_id: String,
    }

    impl Drop for ProjectCleanup {
        fn drop(&mut self) {
            if let Ok(mut conn) = db::connect_readwrite(&self.database_url) {
                let _ = cleanup_project(&mut conn, &self.project_id);
            }
        }
    }

    fn connect_test_db() -> (postgres::Client, String) {
        let database_url = crate::test_env::postgres_test_database_url("prune tests");
        let conn = db::connect_readwrite(&database_url).expect("connect prune PostgreSQL test DB");
        (conn, database_url)
    }

    fn prune_test_context(database_url: String, project_id: &str, qdrant: bool) -> Context {
        Context {
            database_url,
            project_root: std::path::PathBuf::new(),
            project_id: project_id.to_string(),
            quiet: true,
            falkordb: None,
            qdrant: qdrant.then_some(crate::config::QdrantConfig {
                url: Some("http://127.0.0.1:1".to_string()),
                api_key: None,
            }),
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings::default(),
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            index_scope: crate::config::ProjectIndexScope::Single,
        }
    }

    fn unique_test_project_id(prefix: &str) -> String {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time after epoch")
            .as_nanos();
        uuid::Uuid::new_v5(
            &crate::models::CODE_INDEX_UUID_NAMESPACE,
            format!("{prefix}-{nanos}").as_bytes(),
        )
        .to_string()
    }

    fn test_uuid(conn_id: &str, label: &str) -> uuid::Uuid {
        uuid::Uuid::new_v5(
            &crate::models::CODE_INDEX_UUID_NAMESPACE,
            format!("{conn_id}:{label}").as_bytes(),
        )
    }

    fn seed_project_with_child_rows(
        conn: &mut postgres::Client,
        project_id: &str,
        include_project_row: bool,
    ) {
        let file_path = "src/lib.rs";
        let project_uuid = db::id_param(project_id).expect("test project id is a uuid");
        let file_id = test_uuid(project_id, "file");
        let symbol_id = test_uuid(project_id, "symbol");
        let chunk_id = test_uuid(project_id, "chunk");
        if include_project_row {
            conn.execute(
                "INSERT INTO code_indexed_projects
                    (id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms)
                 VALUES ($1, $2, 1, 1, NOW(), 0)",
                &[&project_uuid, &format!("/tmp/{project_id}")],
            )
            .expect("insert indexed project");
        }
        conn.execute(
            "INSERT INTO code_indexed_files
                (id, project_id, file_path, language, content_hash, symbol_count, byte_size)
             VALUES ($1, $2, $3, 'rust', 'hash-1', 1, 19)",
            &[&file_id, &project_uuid, &file_path],
        )
        .expect("insert indexed file");
        conn.execute(
            "INSERT INTO code_symbols
                (id, project_id, file_path, name, qualified_name, kind, language, byte_start,
                 byte_end, line_start, line_end, signature, docstring, parent_symbol_id,
                 content_hash, summary, created_at, updated_at)
             VALUES ($1, $2, $3, 'indexed', 'crate::indexed', 'function', 'rust', 0, 19,
                 1, 1, 'pub fn indexed()', NULL, NULL, 'hash-1', NULL, NOW(), NOW())",
            &[&symbol_id, &project_uuid, &file_path],
        )
        .expect("insert symbol");
        conn.execute(
            "INSERT INTO code_content_chunks
                (id, project_id, file_path, chunk_index, line_start, line_end, content, language)
             VALUES ($1, $2, $3, 0, 1, 1, 'pub fn indexed() {}', 'rust')",
            &[&chunk_id, &project_uuid, &file_path],
        )
        .expect("insert content chunk");
        conn.execute(
            "INSERT INTO code_imports (project_id, source_file, target_module)
             VALUES ($1, $2, 'std::fmt')",
            &[&project_uuid, &file_path],
        )
        .expect("insert import");
        conn.execute(
            "INSERT INTO code_calls
                (project_id, caller_symbol_id, callee_symbol_id, callee_name,
                 callee_target_kind, callee_external_module, file_path, line)
             VALUES ($1, $2, NULL, 'missing', 'unresolved', '', $3, 1)",
            &[&project_uuid, &symbol_id, &file_path],
        )
        .expect("insert call");
    }

    fn cleanup_project(conn: &mut postgres::Client, project_id: &str) -> anyhow::Result<()> {
        let project_id = db::id_param(project_id)?;
        conn.execute(
            "DELETE FROM code_calls WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_imports WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_content_chunks WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_symbols WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_indexed_files WHERE project_id = $1",
            &[&project_id],
        )?;
        conn.execute(
            "DELETE FROM code_indexed_projects WHERE id = $1",
            &[&project_id],
        )?;
        Ok(())
    }

    fn project_child_row_count(conn: &mut postgres::Client, project_id: &str) -> i64 {
        let files = count_rows(conn, "code_indexed_files", project_id);
        let symbols = count_rows(conn, "code_symbols", project_id);
        let chunks = count_rows(conn, "code_content_chunks", project_id);
        let imports = count_rows(conn, "code_imports", project_id);
        let calls = count_rows(conn, "code_calls", project_id);
        files + symbols + chunks + imports + calls
    }

    fn count_rows(conn: &mut postgres::Client, table: &str, project_id: &str) -> i64 {
        conn.query_one(
            &format!("SELECT COUNT(*)::BIGINT FROM {table} WHERE project_id = $1"),
            &[&db::id_param(project_id).expect("test project id is a uuid")],
        )
        .expect("count rows")
        .get(0)
    }
}

#![allow(dead_code)]

use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};

use crate::config::{self, Context};
use crate::db;
use crate::graph::code_graph;
use crate::index_lock::{IndexLockPolicy, lock_project_by_id};
use crate::vector::code_symbols;

use super::content_gc::{ContentGcCandidate, discover_content_gc, prune_content_versions};
use super::invalidate::invalidate_project_locked;
use super::projects::stale_projects;
use super::shared::{collect_projects_from, display_name};

mod inventory;
mod reconcile;

use inventory::{
    CollectionInventory, ScopeInventory, all_collection_orphan_ids, all_scope_orphan_ids,
    classify_collection_inventory, classify_scope_inventory,
};
use reconcile::{ReconcileTotals, SweepOutcome, print_reconcile_totals, sweep_discovered_ids_with};

const GLOBAL_SERVICE_CONTEXT_PROJECT_ID: &str = "00000000-0000-0000-0000-000000000000";
const CODE_INDEX_RETENTION_HOURS: i32 = 24;

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

#[derive(Debug, Clone, PartialEq, Eq)]
struct RegistryProjectState {
    id: String,
    eligible: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct MachineProjectState {
    machine_id: String,
    project_id: String,
    root_path: String,
    eligible: bool,
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
struct DiscoveryCounts {
    scanned: usize,
    active: usize,
}

struct GlobalPruneDiscovery {
    services: Context,
    machine_states: Vec<StaleProjectPlan>,
    machine_state_counts: DiscoveryCounts,
    registry_projects: Vec<StaleProjectPlan>,
    registry_project_counts: DiscoveryCounts,
    collections: Option<CollectionInventory>,
    graph_scopes: Option<ScopeInventory>,
    content_gc_candidates: Vec<ContentGcCandidate>,
}

struct GlobalProjectPruneDiscovery {
    authority: HashSet<String>,
    stale_project_ids: HashSet<String>,
    machine_states: Vec<StaleProjectPlan>,
    machine_state_counts: DiscoveryCounts,
    registry_projects: Vec<StaleProjectPlan>,
    registry_project_counts: DiscoveryCounts,
}

impl GlobalPruneDiscovery {
    fn destructive_set(&self) -> DestructiveSet {
        DestructiveSet {
            stale_project_ids: self
                .machine_states
                .iter()
                .chain(&self.registry_projects)
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
    let _ = quiet;
    let outcome = crate::daemon::post_code_index_prune(force, retention_days)?;
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

fn discover_global_prune(quiet: bool, retention_days: u32) -> anyhow::Result<GlobalPruneDiscovery> {
    let database_url = db::resolve_database_url()?;
    let project_discovery = discover_global_project_prune(&database_url)?;
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
        machine_states: project_discovery.machine_states,
        machine_state_counts: project_discovery.machine_state_counts,
        registry_projects: project_discovery.registry_projects,
        registry_project_counts: project_discovery.registry_project_counts,
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
    let project_discovery = discover_global_project_prune(&discovery.services.database_url)?;
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
    database_url: &str,
) -> anyhow::Result<GlobalProjectPruneDiscovery> {
    let local_machine_id = gobby_core::machine::read_local_machine_id()?;
    let mut conn = db::connect_readonly(database_url)?;
    let registry_projects = conn
        .query(
            "SELECT id::text AS id,
                    updated_at <= NOW() - ($1::INT * INTERVAL '1 hour') AS eligible
             FROM code_indexed_projects
             ORDER BY id",
            &[&CODE_INDEX_RETENTION_HOURS],
        )?
        .into_iter()
        .map(|row| RegistryProjectState {
            id: row.get("id"),
            eligible: row.get("eligible"),
        })
        .collect::<Vec<_>>();
    let machine_states = conn
        .query(
            "SELECT machine_id::text AS machine_id, project_id::text AS project_id, root_path,
                    updated_at <= NOW() - ($1::INT * INTERVAL '1 hour') AS eligible
             FROM code_indexed_project_states
             ORDER BY machine_id, project_id",
            &[&CODE_INDEX_RETENTION_HOURS],
        )?
        .into_iter()
        .map(|row| MachineProjectState {
            machine_id: row.get("machine_id"),
            project_id: row.get("project_id"),
            root_path: row.get("root_path"),
            eligible: row.get("eligible"),
        })
        .collect::<Vec<_>>();
    Ok(classify_global_project_prune(
        &local_machine_id,
        &registry_projects,
        &machine_states,
    ))
}

fn classify_global_project_prune(
    local_machine_id: &str,
    registry_projects: &[RegistryProjectState],
    machine_states: &[MachineProjectState],
) -> GlobalProjectPruneDiscovery {
    let mut authority = registry_projects
        .iter()
        .map(|project| project.id.clone())
        .collect::<HashSet<_>>();
    authority.extend(machine_states.iter().map(|state| state.project_id.clone()));

    let local_states = machine_states
        .iter()
        .filter(|state| state.machine_id == local_machine_id)
        .collect::<Vec<_>>();
    let mut stale_reasons = BTreeMap::new();
    for state in &local_states {
        if let Some(reason) = stale_root_reason(&state.project_id, &state.root_path) {
            stale_reasons.insert(state.project_id.clone(), reason.to_string());
        }
    }

    let mut by_root: BTreeMap<PathBuf, Vec<&MachineProjectState>> = BTreeMap::new();
    for state in &local_states {
        if stale_reasons.contains_key(&state.project_id) {
            continue;
        }
        let Ok(root) = Path::new(&state.root_path).canonicalize() else {
            continue;
        };
        by_root.entry(root).or_default().push(state);
    }
    for (root, states) in by_root {
        if states.len() < 2 {
            continue;
        }
        let Ok(identity) = config::resolve_project_identity(&root, config::MissingIdentity::Error)
        else {
            continue;
        };
        if !states
            .iter()
            .any(|state| state.project_id == identity.project_id)
        {
            continue;
        }
        for state in states {
            if state.project_id != identity.project_id {
                stale_reasons.insert(
                    state.project_id.clone(),
                    format!(
                        "duplicate root superseded by current project id {}",
                        crate::utils::short_id(&identity.project_id)
                    ),
                );
            }
        }
    }

    let machine_state_plans = local_states
        .into_iter()
        .filter(|state| state.eligible)
        .filter_map(|state| {
            stale_reasons
                .get(&state.project_id)
                .map(|reason| StaleProjectPlan {
                    id: state.project_id.clone(),
                    label: project_label(&state.root_path, &state.project_id),
                    reason: reason.clone(),
                })
        })
        .collect::<Vec<_>>();
    let state_project_ids = machine_states
        .iter()
        .map(|state| state.project_id.as_str())
        .collect::<HashSet<_>>();
    let registry_project_plans = registry_projects
        .iter()
        .filter(|project| project.eligible && !state_project_ids.contains(project.id.as_str()))
        .map(|project| StaleProjectPlan {
            id: project.id.clone(),
            label: project.id.clone(),
            reason: format!(
                "registry has no machine state and is at least {CODE_INDEX_RETENTION_HOURS} hours old"
            ),
        })
        .collect::<Vec<_>>();
    let stale_project_ids = machine_state_plans
        .iter()
        .chain(&registry_project_plans)
        .map(|project| project.id.clone())
        .collect();
    let machine_state_counts = DiscoveryCounts {
        scanned: machine_states.len(),
        active: machine_states.len() - machine_state_plans.len(),
    };
    let registry_project_counts = DiscoveryCounts {
        scanned: registry_projects.len(),
        active: registry_projects.len() - registry_project_plans.len(),
    };

    GlobalProjectPruneDiscovery {
        authority,
        stale_project_ids,
        machine_states: machine_state_plans,
        machine_state_counts,
        registry_projects: registry_project_plans,
        registry_project_counts,
    }
}

fn stale_root_reason(project_id: &str, root_path: &str) -> Option<&'static str> {
    if project_id.starts_with("00000000") {
        return Some("sentinel project (not a code project)");
    }
    if root_path.is_empty() {
        return Some("empty root path");
    }
    let root = Path::new(root_path);
    if !root.is_absolute() {
        return Some("relative root path");
    }
    if !root.exists() {
        return Some("path does not exist");
    }
    None
}

fn project_label(root_path: &str, project_id: &str) -> String {
    Path::new(root_path)
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .filter(|name| !name.is_empty())
        .unwrap_or_else(|| project_id.to_string())
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
        machine_state_counts: DiscoveryCounts::default(),
        registry_projects: Vec::new(),
        registry_project_counts: DiscoveryCounts::default(),
        collections: None,
        graph_scopes: None,
        content_gc_candidates,
    })
}

fn confirm_global_prune(discovery: &GlobalPruneDiscovery) -> anyhow::Result<bool> {
    eprintln!(
        "Pending gcode prune: {} stale machine state(s), {} stale registry project(s), {} orphan collection(s), {} orphan graph scope(s), {} expired content version(s).",
        discovery.machine_states.len(),
        discovery.registry_projects.len(),
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
    for project in &discovery.machine_states {
        eprintln!(
            "  machine state: {} ({}) — {}",
            project.label, project.id, project.reason
        );
    }
    for project in &discovery.registry_projects {
        eprintln!(
            "  registry project: {} ({}) — {}",
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

fn mutate_machine_states(discovery: &GlobalPruneDiscovery) -> ReconcileTotals {
    let project_ids = discovery
        .machine_states
        .iter()
        .map(|project| project.id.clone())
        .collect::<Vec<_>>();
    let mut totals = sweep_discovered_ids_with(&project_ids, |project_id| {
        let Some(_lock) = lock_project_by_id(
            &discovery.services.database_url,
            project_id,
            IndexLockPolicy::maintenance_try(),
        )?
        else {
            return Ok(SweepOutcome::Busy);
        };
        reconcile_machine_state_locked(&discovery.services.database_url, project_id)
    });
    totals.scanned = discovery.machine_state_counts.scanned;
    totals.active += discovery.machine_state_counts.active;
    totals
}

fn reconcile_machine_state_locked(
    database_url: &str,
    project_id: &str,
) -> anyhow::Result<SweepOutcome> {
    let mut conn = db::connect_readwrite(database_url)?;
    let local_machine_id = gobby_core::machine::read_local_machine_id()?;
    let machine_id = db::id_param(&local_machine_id)?;
    let project_uuid = db::id_param(project_id)?;
    let Some(row) = conn.query_opt(
        "SELECT root_path,
                updated_at <= NOW() - ($3::INT * INTERVAL '1 hour') AS eligible
         FROM code_indexed_project_states
         WHERE machine_id = $1 AND project_id = $2",
        &[&machine_id, &project_uuid, &CODE_INDEX_RETENTION_HOURS],
    )?
    else {
        return Ok(SweepOutcome::AlreadyMissing);
    };
    let state = MachineProjectState {
        machine_id: local_machine_id,
        project_id: project_id.to_string(),
        root_path: row.get("root_path"),
        eligible: row.get("eligible"),
    };
    if !state.eligible || !machine_state_remains_stale(&mut conn, &state)? {
        return Ok(SweepOutcome::Active);
    }

    let mut tx = conn.transaction()?;
    tx.execute(
        "DELETE FROM code_indexed_file_states
         WHERE machine_id = $1 AND project_id = $2",
        &[&machine_id, &project_uuid],
    )?;
    let deleted = tx.execute(
        "DELETE FROM code_indexed_project_states
         WHERE machine_id = $1 AND project_id = $2
           AND updated_at <= NOW() - ($3::INT * INTERVAL '1 hour')",
        &[&machine_id, &project_uuid, &CODE_INDEX_RETENTION_HOURS],
    )?;
    if deleted == 0 {
        tx.rollback()?;
        return Ok(SweepOutcome::Active);
    }
    let has_remaining_state = tx
        .query_opt(
            "SELECT 1 FROM code_indexed_project_states WHERE project_id = $1 LIMIT 1",
            &[&project_uuid],
        )?
        .is_some();
    if !has_remaining_state {
        tx.execute(
            "DELETE FROM code_indexed_projects WHERE id = $1",
            &[&project_uuid],
        )?;
    }
    tx.commit()?;
    Ok(SweepOutcome::Deleted)
}

fn machine_state_remains_stale(
    conn: &mut postgres::Client,
    state: &MachineProjectState,
) -> anyhow::Result<bool> {
    if stale_root_reason(&state.project_id, &state.root_path).is_some() {
        return Ok(true);
    }
    let Ok(root) = Path::new(&state.root_path).canonicalize() else {
        return Ok(false);
    };
    let Ok(identity) = config::resolve_project_identity(&root, config::MissingIdentity::Error)
    else {
        return Ok(false);
    };
    if identity.project_id == state.project_id {
        return Ok(false);
    }

    let machine_id = db::id_param(&state.machine_id)?;
    let current_project_id = db::id_param(&identity.project_id)?;
    let Some(row) = conn.query_opt(
        "SELECT root_path FROM code_indexed_project_states
         WHERE machine_id = $1 AND project_id = $2",
        &[&machine_id, &current_project_id],
    )?
    else {
        return Ok(false);
    };
    let current_root_path: String = row.get("root_path");
    if stale_root_reason(&identity.project_id, &current_root_path).is_some() {
        return Ok(false);
    }
    let Ok(current_root) = Path::new(&current_root_path).canonicalize() else {
        return Ok(false);
    };
    Ok(current_root == root)
}

fn mutate_registry_projects(discovery: &GlobalPruneDiscovery) -> ReconcileTotals {
    let project_ids = discovery
        .registry_projects
        .iter()
        .map(|project| project.id.clone())
        .collect::<Vec<_>>();
    let mut totals = sweep_discovered_ids_with(&project_ids, |project_id| {
        let Some(_lock) = lock_project_by_id(
            &discovery.services.database_url,
            project_id,
            IndexLockPolicy::maintenance_try(),
        )?
        else {
            return Ok(SweepOutcome::Busy);
        };
        reconcile_registry_project_locked(&discovery.services.database_url, project_id)
    });
    totals.scanned = discovery.registry_project_counts.scanned;
    totals.active += discovery.registry_project_counts.active;
    totals
}

fn reconcile_registry_project_locked(
    database_url: &str,
    project_id: &str,
) -> anyhow::Result<SweepOutcome> {
    let mut conn = db::connect_readwrite(database_url)?;
    let project_uuid = db::id_param(project_id)?;
    let Some(row) = conn.query_opt(
        "SELECT updated_at <= NOW() - ($2::INT * INTERVAL '1 hour') AS eligible,
                EXISTS (
                    SELECT 1 FROM code_indexed_project_states state
                    WHERE state.project_id = project.id
                ) AS has_state
         FROM code_indexed_projects project
         WHERE project.id = $1",
        &[&project_uuid, &CODE_INDEX_RETENTION_HOURS],
    )?
    else {
        return Ok(SweepOutcome::AlreadyMissing);
    };
    if !row.get::<_, bool>("eligible") || row.get::<_, bool>("has_state") {
        return Ok(SweepOutcome::Active);
    }

    let deleted = conn.execute(
        "DELETE FROM code_indexed_projects project
         WHERE project.id = $1
           AND project.updated_at <= NOW() - ($2::INT * INTERVAL '1 hour')
           AND NOT EXISTS (
               SELECT 1 FROM code_indexed_project_states state
               WHERE state.project_id = project.id
           )",
        &[&project_uuid, &CODE_INDEX_RETENTION_HOURS],
    )?;
    Ok(if deleted == 0 {
        SweepOutcome::Active
    } else {
        SweepOutcome::Deleted
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

use postgres::GenericClient;
use std::collections::HashSet;

use crate::config::{self, Context};
use crate::db;
use crate::graph::code_graph;
use crate::index::indexer;
use crate::utils::short_id;
use crate::vector::code_symbols;

use super::projects::stale_projects;
use super::shared::{collect_projects, display_name};

const ORPHAN_PROJECT_WARNING_LIMIT: usize = 5;
const ORPHAN_PROJECT_SUMMARY_LIMIT: usize = 8;

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
    if prune_stale_projects(force)?.is_none() {
        return Ok(());
    }

    let orphan_totals = reconcile_orphan_projects(quiet)?;
    print_orphan_project_reconcile_totals(&orphan_totals);

    match projection_cleanup_scope(project_override) {
        ProjectionCleanupScope::AllIndexedProjects => prune_all_project_projections(quiet),
        ProjectionCleanupScope::ResolvedProjectOverride => {
            prune_resolved_project_projections(project_override, quiet)
        }
    }
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

fn prune_stale_projects(force: bool) -> anyhow::Result<Option<usize>> {
    let all_projects = collect_projects()?;
    let stale = stale_projects(&all_projects);

    if stale.is_empty() {
        eprintln!("No stale projects found.");
        return Ok(Some(0));
    }

    eprintln!("Found {} stale project(s):", stale.len());
    for stale_project in &stale {
        eprintln!(
            "  {} — {}",
            display_name(stale_project.project),
            stale_project.reason
        );
    }

    if !force {
        eprint!("\nRemove these entries and their indexed data? [y/N] ");
        let _ = std::io::Write::flush(&mut std::io::stderr());

        let mut input = String::new();
        std::io::stdin().read_line(&mut input)?;
        if !input.trim().eq_ignore_ascii_case("y") {
            eprintln!("Aborted.");
            return Ok(None);
        }
    }

    let daemon_url = gobby_core::daemon_url::daemon_url();
    let database_url = db::resolve_database_url()?;
    let mut conn = db::connect_readwrite(&database_url)?;

    for stale_project in &stale {
        indexer::invalidate(&mut conn, &stale_project.project.id, Some(&daemon_url))?;
    }

    eprintln!("Pruned {} stale project(s).", stale.len());
    Ok(Some(stale.len()))
}

fn reconcile_orphan_projects(quiet: bool) -> anyhow::Result<OrphanProjectReconcileTotals> {
    let database_url = db::resolve_database_url()?;
    let mut conn = db::connect_readwrite(&database_url)?;
    let project_ids = collect_orphan_project_ids(&mut conn)?;
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

    rows.into_iter()
        .map(|row| row.try_get(0).map_err(anyhow::Error::from))
        .collect()
}

pub(super) fn delete_orphan_project_sql_rows(
    conn: &mut impl GenericClient,
    project_id: &str,
) -> anyhow::Result<OrphanSqlDeletionCounts> {
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

    fn unique_test_project_id(prefix: &str) -> String {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time after epoch")
            .as_nanos();
        format!("{prefix}-{nanos}")
    }

    fn seed_project_with_child_rows(
        conn: &mut postgres::Client,
        project_id: &str,
        include_project_row: bool,
    ) {
        let file_path = "src/lib.rs";
        let file_id = format!("{project_id}-file");
        let symbol_id = format!("{project_id}-symbol");
        if include_project_row {
            conn.execute(
                "INSERT INTO code_indexed_projects
                    (id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms)
                 VALUES ($1, $2, 1, 1, NOW(), 0)",
                &[&project_id, &format!("/tmp/{project_id}")],
            )
            .expect("insert indexed project");
        }
        conn.execute(
            "INSERT INTO code_indexed_files
                (id, project_id, file_path, language, content_hash, symbol_count, byte_size)
             VALUES ($1, $2, $3, 'rust', 'hash-1', 1, 19)",
            &[&file_id, &project_id, &file_path],
        )
        .expect("insert indexed file");
        conn.execute(
            "INSERT INTO code_symbols
                (id, project_id, file_path, name, qualified_name, kind, language, byte_start,
                 byte_end, line_start, line_end, signature, docstring, parent_symbol_id,
                 content_hash, summary, created_at, updated_at)
             VALUES ($1, $2, $3, 'indexed', 'crate::indexed', 'function', 'rust', 0, 19,
                 1, 1, 'pub fn indexed()', NULL, NULL, 'hash-1', NULL, NOW(), NOW())",
            &[&symbol_id, &project_id, &file_path],
        )
        .expect("insert symbol");
        conn.execute(
            "INSERT INTO code_content_chunks
                (id, project_id, file_path, chunk_index, line_start, line_end, content, language)
             VALUES ($1, $2, $3, 0, 1, 1, 'pub fn indexed() {}', 'rust')",
            &[&format!("{project_id}-chunk"), &project_id, &file_path],
        )
        .expect("insert content chunk");
        conn.execute(
            "INSERT INTO code_imports (project_id, source_file, target_module)
             VALUES ($1, $2, 'std::fmt')",
            &[&project_id, &file_path],
        )
        .expect("insert import");
        conn.execute(
            "INSERT INTO code_calls
                (project_id, caller_symbol_id, callee_symbol_id, callee_name,
                 callee_target_kind, callee_external_module, file_path, line)
             VALUES ($1, $2, '', 'missing', 'unresolved', '', $3, 1)",
            &[&project_id, &symbol_id, &file_path],
        )
        .expect("insert call");
    }

    fn cleanup_project(conn: &mut postgres::Client, project_id: &str) -> anyhow::Result<()> {
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
            &[&project_id],
        )
        .expect("count rows")
        .get(0)
    }
}

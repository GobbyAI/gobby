//! Acceptance tests for the Primary index split (#24673, plan 1.4).
//!
//! Each test drives the node half in-process (`node::split`) and the hub's database
//! half (`node::hub::plan_index` and `apply_index`) against the isolated test database.
//! Every project gets its own checkout root, because `project_checkouts` is unique per
//! machine and root.

use std::path::Path;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::config::{CodeVectorSettings, Context, ProjectIndexScope};
use crate::db;
use crate::index::hasher::content_hash;
use crate::index::indexer::{IndexOptions, IndexOutcome, IndexRequest, index_files};
use crate::index_lock::project_lock_key;
use crate::node::hub::{HubScope, apply_index, plan_index};
use crate::node::split::{index_files_in_two_halves, parse_facts};
use crate::node::{
    FileHash, FinalBatch, IndexApplyRequest, IndexApplyResponse, IndexPlanRequest,
    IndexPlanResponse, NodeScope, ParsedFileFacts, RouteError,
};

const FIXTURE_PATHS: [&str; 2] = ["src/lib.rs", "src/util.rs"];

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn local_index_is_unchanged_by_the_split() -> anyhow::Result<()> {
    let mut h = Harness::new()?;
    let local = h.register("local")?;
    let split = h.register("split")?;

    let before = index_files(local.request(true), &h.ctx(&local), IndexOptions::default())?;
    let after =
        index_files_in_two_halves(split.request(true), &h.ctx(&split), IndexOptions::default())?;

    assert_eq!(before.indexed_files, 2, "the fixture must index both files");
    assert_eq!(outcome_counts(&after), outcome_counts(&before));
    assert_eq!(h.rows(&split)?, h.rows(&local)?);
    Ok(())
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn plan_and_apply_match_a_local_index() -> anyhow::Result<()> {
    let mut h = Harness::new()?;
    let local = h.register("foreign-local")?;
    let split = h.register("foreign-split")?;
    let local_outcome = index_files(local.request(true), &h.ctx(&local), IndexOptions::default())?;

    let files = split.hashes();
    let plan = h.plan(&split, true, &files)?;
    assert_eq!(sorted(plan.parse.clone()), FIXTURE_PATHS);
    let facts = parse_facts(&h.ctx(&split), &split.request(true), &plan.parse)?;
    let response = h.apply(&split, true, facts, Some(final_batch(&files, &plan.parse)))?;

    assert!(response.reparse.is_empty(), "{:?}", response.reparse);
    let outcome = response
        .outcome
        .expect("a completed run returns its outcome");
    assert_eq!(outcome_counts(&outcome), outcome_counts(&local_outcome));
    assert_eq!(h.rows(&split)?, h.rows(&local)?);
    Ok(())
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn plan_lists_only_paths_to_parse() -> anyhow::Result<()> {
    let mut h = Harness::new()?;
    let project = h.register("plan")?;
    index_files(
        project.request(true),
        &h.ctx(&project),
        IndexOptions::default(),
    )?;
    h.mark_synced(&project)?;

    // util.rs loses its machine state, but its facts are still adoptable: not parsed.
    h.forget_state(&project, "src/util.rs")?;
    let plan = h.plan(&project, false, &project.hashes())?;
    assert!(plan.parse.is_empty(), "adoptable paths: {:?}", plan.parse);

    // New content for util.rs has no adoptable facts: it must be parsed.
    project.write("src/util.rs", "pub fn beta() -> u8 { 2 }\n");
    let plan = h.plan(&project, false, &project.hashes())?;
    assert_eq!(plan.parse, vec!["src/util.rs"]);

    // A full run lists every present path, adoptable or not.
    let plan = h.plan(&project, true, &project.hashes())?;
    assert_eq!(sorted(plan.parse), FIXTURE_PATHS);
    Ok(())
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn apply_returns_reparse_and_completes_on_retry() -> anyhow::Result<()> {
    let mut h = Harness::new()?;
    let project = h.register("reparse")?;
    let local = h.register("reparse-local")?;
    index_files(
        project.request(true),
        &h.ctx(&project),
        IndexOptions::default(),
    )?;
    h.mark_synced(&project)?;
    h.forget_state(&project, "src/lib.rs")?;

    let files = project.hashes();
    let plan = h.plan(&project, false, &files)?;
    assert!(plan.parse.is_empty(), "adoptable paths: {:?}", plan.parse);

    // Between plan and apply, the adoptable facts for lib.rs disappear.
    h.forget_facts(&project, "src/lib.rs")?;
    let first = h.apply(&project, false, Vec::new(), Some(final_batch(&files, &[])))?;
    assert_eq!(first.reparse, vec!["src/lib.rs"]);
    assert!(
        first.outcome.is_none(),
        "a non-empty reparse skips the post-pass"
    );

    // The node parses the reported path and sends one more final batch with its facts.
    let facts = parse_facts(&h.ctx(&project), &project.request(false), &first.reparse)?;
    let retry = h.apply(
        &project,
        false,
        facts,
        Some(final_batch(&files, &first.reparse)),
    )?;
    assert!(retry.reparse.is_empty(), "{:?}", retry.reparse);
    let outcome = retry.outcome.expect("the retry completes the run");
    assert!(
        outcome
            .indexed_file_paths
            .contains(&"src/lib.rs".to_string())
    );

    index_files(local.request(true), &h.ctx(&local), IndexOptions::default())?;
    assert_eq!(h.rows(&project)?, h.rows(&local)?);
    Ok(())
}

#[test]
#[cfg_attr(
    not(gcode_postgres_tests),
    ignore = "requires the PostgreSQL test database"
)]
#[serial_test::serial(serial_db)]
fn apply_keeps_the_checkout_fence_and_bounded_lock() -> anyhow::Result<()> {
    let mut h = Harness::new()?;
    let project = h.unregistered("fence");
    let files = project.hashes();

    // No project_checkouts row for this machine: refused, and nothing is written.
    let error = h
        .apply_raw(&project, true, Vec::new(), Some(final_batch(&files, &[])))
        .expect_err("an unregistered checkout must be refused");
    assert_eq!(error.code, "checkout_required");
    assert_eq!(h.written_rows(&project)?, 0);

    // With the checkout registered, a lock held by another session must come back busy
    // after the bounded wait, and still write nothing.
    h.register_checkout(&project)?;
    let key = project_lock_key(&project.id);
    let mut holder = db::connect_readwrite(&h.database_url)?;
    holder.query_one("SELECT pg_advisory_lock($1)", &[&key])?;
    let started = Instant::now();
    let result = h.apply_raw(&project, true, Vec::new(), Some(final_batch(&files, &[])));
    let waited = started.elapsed();
    holder.query_one("SELECT pg_advisory_unlock($1)", &[&key])?;

    let error = result.expect_err("a held project lock must come back busy");
    assert_eq!(error.code, "index_busy");
    assert!(
        waited < Duration::from_secs(120),
        "the wait must be bounded; it ran for {waited:?}"
    );
    assert_eq!(h.written_rows(&project)?, 0);
    Ok(())
}

// --- Harness -----------------------------------------------------------------------

/// One project with its own checkout root holding the fixture.
struct Project {
    id: String,
    root: tempfile::TempDir,
}

impl Project {
    fn write(&self, rel: &str, contents: &str) {
        let path = self.root.path().join(rel);
        std::fs::create_dir_all(path.parent().expect("file has a parent")).expect("create dir");
        std::fs::write(path, contents).expect("write fixture file");
    }

    fn request(&self, full: bool) -> IndexRequest {
        IndexRequest {
            project_root: self.root.path().to_path_buf(),
            path_filter: None,
            explicit_files: Vec::new(),
            full,
            require_cpp_semantics: false,
            sync_projections: false,
        }
    }

    /// The hashes a node reports for the fixture's current contents.
    fn hashes(&self) -> Vec<FileHash> {
        FIXTURE_PATHS
            .iter()
            .map(|path| {
                let bytes = std::fs::read(self.root.path().join(path)).expect("read fixture file");
                FileHash {
                    path: (*path).to_string(),
                    content_hash: content_hash(&bytes),
                    git_blob: None,
                }
            })
            .collect()
    }
}

struct Harness {
    database_url: String,
    conn: postgres::Client,
    machine_id: String,
    projects: Vec<String>,
}

impl Harness {
    fn new() -> anyhow::Result<Self> {
        let database_url = crate::test_env::postgres_test_database_url("node index split tests");
        let mut conn = db::connect_readwrite(&database_url)?;
        // A synthetic machine: the hub never reads the local daemon's machine id.
        let machine_id = unique_id("node-split-machine");
        crate::test_env::seed_test_machine(&mut conn, &machine_id).map_err(anyhow::Error::msg)?;
        Ok(Self {
            database_url,
            conn,
            machine_id,
            projects: Vec::new(),
        })
    }

    /// A project with an unregistered checkout: no `project_checkouts` row yet.
    fn unregistered(&mut self, label: &str) -> Project {
        let id = unique_id(label);
        self.projects.push(id.clone());
        let parent = std::env::temp_dir()
            .canonicalize()
            .expect("canonical temp root");
        let project = Project {
            id,
            root: tempfile::tempdir_in(parent).expect("project tempdir"),
        };
        project.write(
            "src/lib.rs",
            "mod util;\npub fn alpha() -> u8 { util::beta() }\n",
        );
        project.write("src/util.rs", "pub fn beta() -> u8 { 1 }\n");
        project
    }

    /// A project whose checkout is registered for this machine at its own root.
    fn register(&mut self, label: &str) -> anyhow::Result<Project> {
        let project = self.unregistered(label);
        self.register_checkout(&project)?;
        Ok(project)
    }

    fn register_checkout(&mut self, project: &Project) -> anyhow::Result<()> {
        seed_checkout(
            &mut self.conn,
            &self.machine_id,
            &project.id,
            project.root.path(),
        )
    }

    fn ctx(&self, project: &Project) -> Context {
        Context {
            machine_id: self.machine_id.clone(),
            database_url: self.database_url.clone(),
            project_root: project.root.path().to_path_buf(),
            project_id: project.id.clone(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: CodeVectorSettings::default(),
            runtime_config_capture_degraded: false,
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            grant_ai: None,
            index_scope: ProjectIndexScope::Single,
        }
    }

    fn hub(&self, project: &Project) -> HubScope {
        HubScope {
            database_url: self.database_url.clone(),
            machine_id: self.machine_id.clone(),
            project_id: project.id.clone(),
            overlay_project_id: None,
        }
    }

    fn node_scope(project: &Project) -> NodeScope {
        NodeScope {
            root: project.root.path().to_string_lossy().into_owned(),
            parent_root: None,
        }
    }

    fn plan_raw(
        &self,
        project: &Project,
        full: bool,
        files: &[FileHash],
    ) -> Result<IndexPlanResponse, RouteError> {
        plan_index(
            &self.hub(project),
            IndexPlanRequest {
                scope: Self::node_scope(project),
                full,
                path_filter: None,
                files: files.to_vec(),
            },
        )
    }

    fn plan(
        &self,
        project: &Project,
        full: bool,
        files: &[FileHash],
    ) -> anyhow::Result<IndexPlanResponse> {
        self.plan_raw(project, full, files).map_err(route_error)
    }

    fn apply_raw(
        &self,
        project: &Project,
        full: bool,
        files: Vec<ParsedFileFacts>,
        final_batch: Option<FinalBatch>,
    ) -> Result<IndexApplyResponse, RouteError> {
        apply_index(
            &self.hub(project),
            IndexApplyRequest {
                scope: Self::node_scope(project),
                full,
                path_filter: None,
                files,
                final_batch,
            },
        )
    }

    fn apply(
        &self,
        project: &Project,
        full: bool,
        files: Vec<ParsedFileFacts>,
        final_batch: Option<FinalBatch>,
    ) -> anyhow::Result<IndexApplyResponse> {
        self.apply_raw(project, full, files, final_batch)
            .map_err(route_error)
    }

    /// Marks every indexed file's projections synced, so its facts are adoptable.
    fn mark_synced(&mut self, project: &Project) -> anyhow::Result<()> {
        let id = db::id_param(&project.id)?;
        self.conn.execute(
            "UPDATE code_indexed_files SET graph_synced = true, vectors_synced = true
             WHERE project_id = $1",
            &[&id],
        )?;
        Ok(())
    }

    /// Removes the machine's state for one file, so the plan sees it as stale.
    fn forget_state(&mut self, project: &Project, file_path: &str) -> anyhow::Result<()> {
        let id = db::id_param(&project.id)?;
        let machine = db::id_param(&self.machine_id)?;
        self.conn.execute(
            "DELETE FROM code_indexed_file_states
             WHERE machine_id = $1 AND project_id = $2 AND file_path = $3",
            &[&machine, &id, &file_path],
        )?;
        Ok(())
    }

    /// Removes one file's adoptable facts: its indexed-file row and everything derived from it.
    fn forget_facts(&mut self, project: &Project, file_path: &str) -> anyhow::Result<()> {
        let id = db::id_param(&project.id)?;
        for table in [
            "code_calls",
            "code_inheritance",
            "code_content_chunks",
            "code_symbols",
            "code_indexed_files",
        ] {
            self.conn.execute(
                &format!("DELETE FROM {table} WHERE project_id = $1 AND file_path = $2"),
                &[&id, &file_path],
            )?;
        }
        Ok(())
    }

    /// Every row a run writes for the project, excluding ids, timestamps and roots.
    fn rows(&mut self, project: &Project) -> anyhow::Result<Rows> {
        let id = db::id_param(&project.id)?;
        let machine = db::id_param(&self.machine_id)?;
        Ok(Rows {
            files: text_rows(
                &mut self.conn,
                "SELECT concat_ws('|', file_path, language, content_hash,
                        symbol_count::text, byte_size::text)
                 FROM code_indexed_files WHERE project_id = $1 ORDER BY file_path",
                &[&id],
            )?,
            states: text_rows(
                &mut self.conn,
                "SELECT concat_ws('|', file_path, content_hash)
                 FROM code_indexed_file_states
                 WHERE project_id = $1 AND machine_id = $2 ORDER BY file_path",
                &[&id, &machine],
            )?,
            symbols: text_rows(
                &mut self.conn,
                "SELECT concat_ws('|', file_path, qualified_name, kind, line_start::text,
                        line_end::text, file_content_hash, content_hash)
                 FROM code_symbols WHERE project_id = $1
                 ORDER BY file_path, qualified_name, line_start",
                &[&id],
            )?,
            chunks: text_rows(
                &mut self.conn,
                "SELECT concat_ws('|', file_path, chunk_index::text, line_start::text,
                        line_end::text, content_hash)
                 FROM code_content_chunks WHERE project_id = $1
                 ORDER BY file_path, chunk_index",
                &[&id],
            )?,
        })
    }

    /// Rows a refused or busy apply could have written for the project.
    fn written_rows(&mut self, project: &Project) -> anyhow::Result<i64> {
        let id = db::id_param(&project.id)?;
        let row = self.conn.query_one(
            "SELECT (SELECT COUNT(*) FROM code_indexed_files WHERE project_id = $1)
                  + (SELECT COUNT(*) FROM code_indexed_file_states WHERE project_id = $1)
                  + (SELECT COUNT(*) FROM code_symbols WHERE project_id = $1)
                  + (SELECT COUNT(*) FROM code_content_chunks WHERE project_id = $1)
                  + (SELECT COUNT(*) FROM code_indexed_projects WHERE id = $1)",
            &[&id],
        )?;
        Ok(row.get(0))
    }
}

impl Drop for Harness {
    fn drop(&mut self) {
        for project_id in &self.projects {
            let _ = cleanup_project(&mut self.conn, project_id);
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
struct Rows {
    files: Vec<String>,
    states: Vec<String>,
    symbols: Vec<String>,
    chunks: Vec<String>,
}

#[derive(Debug, PartialEq, Eq)]
struct OutcomeCounts {
    scanned_files: usize,
    indexed_files: usize,
    skipped_files: usize,
    symbols_indexed: usize,
    imports_indexed: usize,
    calls_indexed: usize,
    unresolved_targets_indexed: usize,
    chunks_indexed: usize,
    tombstones_indexed: usize,
    indexed_file_paths: Vec<String>,
}

fn outcome_counts(outcome: &IndexOutcome) -> OutcomeCounts {
    OutcomeCounts {
        scanned_files: outcome.scanned_files,
        indexed_files: outcome.indexed_files,
        skipped_files: outcome.skipped_files,
        symbols_indexed: outcome.symbols_indexed,
        imports_indexed: outcome.imports_indexed,
        calls_indexed: outcome.calls_indexed,
        unresolved_targets_indexed: outcome.unresolved_targets_indexed,
        chunks_indexed: outcome.chunks_indexed,
        tombstones_indexed: outcome.tombstones_indexed,
        indexed_file_paths: sorted(outcome.indexed_file_paths.clone()),
    }
}

fn final_batch(files: &[FileHash], indexed_paths: &[String]) -> FinalBatch {
    FinalBatch {
        present: files.to_vec(),
        indexed_paths: indexed_paths.to_vec(),
        eligible_files: files.len(),
        scanned_files: files.len(),
        unsupported_file_types: Vec::new(),
    }
}

fn route_error(error: RouteError) -> anyhow::Error {
    anyhow::anyhow!("{}: {}", error.code, error.message)
}

fn sorted(mut values: Vec<String>) -> Vec<String> {
    values.sort();
    values
}

fn text_rows(
    conn: &mut postgres::Client,
    sql: &str,
    params: &[&(dyn postgres::types::ToSql + Sync)],
) -> anyhow::Result<Vec<String>> {
    Ok(conn
        .query(sql, params)?
        .iter()
        .map(|row| row.get::<_, String>(0))
        .collect())
}

fn seed_checkout(
    conn: &mut postgres::Client,
    machine_id: &str,
    project_id: &str,
    root: &Path,
) -> anyhow::Result<()> {
    let machine = db::id_param(machine_id)?;
    let project = db::id_param(project_id)?;
    let root = root.to_string_lossy().into_owned();
    conn.execute(
        "INSERT INTO projects (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
        &[&project, &format!("node-split-{project_id}")],
    )?;
    conn.execute(
        "INSERT INTO project_checkouts (machine_id, project_id, root_path)
         VALUES ($1, $2, $3)
         ON CONFLICT (machine_id, project_id) DO UPDATE SET
            root_path = EXCLUDED.root_path,
            updated_at = now()",
        &[&machine, &project, &root],
    )?;
    Ok(())
}

fn cleanup_project(conn: &mut postgres::Client, project_id: &str) -> anyhow::Result<()> {
    let project = db::id_param(project_id)?;
    for table in [
        "code_indexed_file_states",
        "code_indexed_project_states",
        "code_calls",
        "code_inheritance",
        "code_imports",
        "code_content_chunks",
        "code_symbols",
        "code_indexed_files",
    ] {
        conn.execute(
            &format!("DELETE FROM {table} WHERE project_id = $1"),
            &[&project],
        )?;
    }
    conn.execute(
        "DELETE FROM code_indexed_projects WHERE id = $1",
        &[&project],
    )?;
    conn.execute(
        "DELETE FROM project_checkouts WHERE project_id = $1",
        &[&project],
    )?;
    conn.execute("DELETE FROM projects WHERE id = $1", &[&project])?;
    Ok(())
}

fn unique_id(prefix: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time after epoch")
        .as_nanos();
    uuid::Uuid::new_v5(
        &crate::models::CODE_INDEX_UUID_NAMESPACE,
        format!("{prefix}-{}-{nanos}", std::process::id()).as_bytes(),
    )
    .to_string()
}

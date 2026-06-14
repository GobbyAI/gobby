use std::path::{Path, PathBuf};
use std::time::SystemTime;

use crate::config::Context;
use crate::db;
use crate::index::{api, hasher};
use crate::index_lock::{self, IndexLockPolicy, IndexLockResult};
use crate::models::Symbol;
use crate::visibility;

const INFLIGHT_ENV: &str = "GCODE_FRESHNESS_INFLIGHT";

pub enum FreshnessScope {
    Project,
    Files(Vec<PathBuf>),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FreshnessStatus {
    Checked,
    SkippedBusy,
}

pub fn ensure_fresh(ctx: &Context, scope: FreshnessScope) -> anyhow::Result<FreshnessStatus> {
    if std::env::var_os(INFLIGHT_ENV).is_some() {
        return Ok(FreshnessStatus::Checked);
    }

    // Lock-free pre-gate for whole-project reads: if nothing on disk is newer
    // than the recorded index and nothing was deleted, skip the advisory lock
    // and the full re-hash entirely (no "refresh already running" warning).
    // `FreshnessScope::Files` is already cheap (explicit-files path) and is left
    // untouched.
    if matches!(scope, FreshnessScope::Project) && !project_needs_refresh(ctx)? {
        return Ok(FreshnessStatus::Checked);
    }

    let _guard = FreshnessGuard::enter();
    let result =
        index_lock::with_project_lock(ctx, IndexLockPolicy::brief_freshness_try(), || {
            match scope {
                FreshnessScope::Project => {
                    api::index_files(
                        api::IndexRequest {
                            project_root: ctx.project_root.clone(),
                            path_filter: None,
                            explicit_files: Vec::new(),
                            full: false,
                            require_cpp_semantics: false,
                            sync_projections: false,
                        },
                        ctx,
                    )?;
                }
                FreshnessScope::Files(paths) => {
                    let files: Vec<PathBuf> = paths
                        .iter()
                        .map(|path| normalize_file_path(&ctx.project_root, path))
                        .map(PathBuf::from)
                        .collect();
                    if !files.is_empty() {
                        api::index_files(
                            api::IndexRequest {
                                project_root: ctx.project_root.clone(),
                                path_filter: None,
                                explicit_files: files,
                                full: false,
                                require_cpp_semantics: false,
                                sync_projections: false,
                            },
                            ctx,
                        )?;
                    }
                }
            }
            Ok(())
        })?;

    match result {
        IndexLockResult::Acquired(()) => Ok(FreshnessStatus::Checked),
        IndexLockResult::Busy => Ok(FreshnessStatus::SkippedBusy),
    }
}

/// Read-only pre-gate for whole-project freshness.
///
/// Returns `true` when the project must be reconciled under the advisory lock —
/// because it has never been indexed or because the on-disk tree changed since
/// `last_indexed_at` — and `false` when the recorded index is already current
/// and the lock (plus the full re-hash) can be skipped. Reads only; needs the
/// hub exactly like the existing refresh path, and propagates a hub error the
/// same way (`--no-freshness` still bypasses it upstream).
fn project_needs_refresh(ctx: &Context) -> anyhow::Result<bool> {
    let mut conn = db::connect_readonly(&ctx.database_url)?;

    let last_indexed_at: Option<SystemTime> = match conn.query_opt(
        "SELECT last_indexed_at FROM code_indexed_projects WHERE id = $1",
        &[&ctx.project_id],
    )? {
        Some(row) => row.try_get::<_, Option<SystemTime>>(0)?,
        None => None,
    };

    // Never indexed (or no recorded timestamp): do not gate; let the existing
    // refresh path build the first index.
    let Some(last_indexed_at) = last_indexed_at else {
        return Ok(true);
    };

    let indexed_paths = db::list_indexed_file_paths(&mut conn, &ctx.project_id)?;
    drop(conn);

    Ok(api::project_changed_since(
        &ctx.project_root,
        last_indexed_at,
        &indexed_paths,
        crate::index::walker::DiscoveryOptions {
            respect_gitignore: ctx.indexing.respect_gitignore,
        },
    ))
}

pub fn ensure_symbol_fresh(ctx: &Context, id: &str) -> anyhow::Result<FreshnessStatus> {
    if std::env::var_os(INFLIGHT_ENV).is_some() {
        return Ok(FreshnessStatus::Checked);
    }

    let mut conn = db::connect_readonly(&ctx.database_url)?;
    let sym = visibility::visible_symbol_by_id(&mut conn, ctx, id)?;
    drop(conn);

    let Some(sym) = sym else {
        return Ok(FreshnessStatus::Checked);
    };

    if symbol_slice_is_current(ctx, &sym) {
        return Ok(FreshnessStatus::Checked);
    }

    ensure_fresh(
        ctx,
        FreshnessScope::Files(vec![PathBuf::from(&sym.file_path)]),
    )
}

fn symbol_slice_is_current(ctx: &Context, sym: &Symbol) -> bool {
    if sym.content_hash.is_empty() {
        return false;
    }

    let file_path = ctx.project_root.join(&sym.file_path);
    let source = match std::fs::read(file_path) {
        Ok(source) => source,
        Err(_) => return false,
    };

    hasher::symbol_content_hash(&source, sym.byte_start, sym.byte_end)
        .map(|hash| hash == sym.content_hash)
        .unwrap_or(false)
}

fn normalize_file_path(root: &Path, path: &Path) -> String {
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    };

    abs.canonicalize()
        .ok()
        .and_then(|canonical| {
            root.canonicalize().ok().and_then(|canonical_root| {
                canonical
                    .strip_prefix(canonical_root)
                    .ok()
                    .map(Path::to_path_buf)
            })
        })
        .unwrap_or_else(|| path.to_path_buf())
        .to_string_lossy()
        .to_string()
}

struct FreshnessGuard;

impl FreshnessGuard {
    fn enter() -> Self {
        // SAFETY: gcode runs freshness indexing synchronously in this CLI process
        // and restores the variable before returning to command dispatch.
        unsafe { std::env::set_var(INFLIGHT_ENV, "1") };
        Self
    }
}

impl Drop for FreshnessGuard {
    fn drop(&mut self) {
        // SAFETY: see FreshnessGuard::enter.
        unsafe { std::env::remove_var(INFLIGHT_ENV) };
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::CODE_INDEX_UUID_NAMESPACE;
    use postgres::Client;

    fn context_for(root: &Path) -> Context {
        Context {
            database_url: "postgresql://localhost/gobby-test".to_string(),
            project_root: root.to_path_buf(),
            project_id: "proj".to_string(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings::default(),
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            index_scope: crate::config::ProjectIndexScope::Single,
        }
    }

    fn symbol_hash(source: &[u8], start: usize, end: usize) -> String {
        hasher::symbol_content_hash(source, start, end).expect("symbol hash")
    }

    fn postgres_test_context(project_id: &str) -> Context {
        let database_url = std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL")
            .expect("GCODE_POSTGRES_TEST_DATABASE_URL must be set for freshness tests");
        db::connect_readwrite(&database_url).expect("connect freshness PostgreSQL test database");
        Context {
            database_url,
            project_root: std::path::PathBuf::from("/tmp/gcode-freshness-lock-test"),
            project_id: project_id.to_string(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings::default(),
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            index_scope: crate::config::ProjectIndexScope::Single,
        }
    }

    fn postgres_context_with_root(project_id: &str, root: &Path) -> Context {
        let database_url = std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL")
            .expect("GCODE_POSTGRES_TEST_DATABASE_URL must be set for freshness tests");
        db::connect_readwrite(&database_url).expect("connect freshness PostgreSQL test database");
        Context {
            database_url,
            project_root: root.to_path_buf(),
            project_id: project_id.to_string(),
            quiet: true,
            falkordb: None,
            qdrant: None,
            embedding: None,
            code_vectors: crate::config::CodeVectorSettings::default(),
            indexing: gobby_core::config::IndexingConfig::default(),
            daemon_url: None,
            index_scope: crate::config::ProjectIndexScope::Single,
        }
    }

    fn hold_project_lock(ctx: &Context) -> Client {
        let mut conn =
            db::connect_readwrite(&ctx.database_url).expect("connect test PostgreSQL hub");
        let key = crate::index_lock::project_lock_key(&ctx.project_id);
        conn.execute("SELECT pg_advisory_lock($1)", &[&key])
            .expect("hold project advisory lock");
        conn
    }

    fn set_mtime(path: &Path, time: SystemTime) {
        std::fs::File::options()
            .read(true)
            .write(true)
            .open(path)
            .expect("open file to set mtime")
            .set_modified(time)
            .expect("set mtime");
    }

    fn invalidate_test_project(ctx: &Context) {
        let mut conn =
            db::connect_readwrite(&ctx.database_url).expect("connect test PostgreSQL hub");
        crate::index::indexer::invalidate(&mut conn, &ctx.project_id, None)
            .expect("invalidate test project index");
    }

    fn full_index(ctx: &Context) {
        api::index_files(
            api::IndexRequest {
                project_root: ctx.project_root.clone(),
                path_filter: None,
                explicit_files: Vec::new(),
                full: true,
                require_cpp_semantics: false,
                sync_projections: false,
            },
            ctx,
        )
        .expect("full index of test project");
    }

    mod serial_db {
        use super::*;

        #[test]
        #[serial_test::serial(serial_db)]
        fn no_freshness_env_short_circuits_project_refresh() {
            let tmp = tempfile::tempdir().expect("tempdir");
            let ctx = context_for(tmp.path());
            unsafe { std::env::set_var(INFLIGHT_ENV, "1") };
            let result = ensure_fresh(&ctx, FreshnessScope::Project);
            unsafe { std::env::remove_var(INFLIGHT_ENV) };

            assert_eq!(result.expect("freshness status"), FreshnessStatus::Checked);
        }

        #[test]
        #[cfg_attr(
            not(gcode_postgres_tests),
            ignore = "requires GCODE_POSTGRES_TEST_DATABASE_URL"
        )]
        #[serial_test::serial(serial_db)]
        fn busy_project_lock_skips_freshness_refresh() {
            let ctx = postgres_test_context("gcode-freshness-busy");
            let _holder = hold_project_lock(&ctx);

            let status = ensure_fresh(&ctx, FreshnessScope::Project).expect("freshness status");

            assert_eq!(status, FreshnessStatus::SkippedBusy);
        }

        #[test]
        #[cfg_attr(
            not(gcode_postgres_tests),
            ignore = "requires GCODE_POSTGRES_TEST_DATABASE_URL"
        )]
        #[serial_test::serial(serial_db)]
        fn pre_gate_skips_lock_when_unchanged_and_trips_after_a_change() {
            let tmp = tempfile::tempdir().expect("tempdir");
            let root = tmp.path();
            std::fs::create_dir_all(root.join("src")).expect("create src");
            let lib = root.join("src/lib.rs");
            std::fs::write(&lib, b"fn main() {}\n").expect("write lib.rs");
            std::fs::write(root.join("README.md"), b"# Title\n").expect("write README");

            // Age the files well past the skew margin so a clean index leaves them
            // unambiguously older than last_indexed_at, regardless of host/hub skew.
            let aged = SystemTime::now() - std::time::Duration::from_secs(3600);
            set_mtime(&lib, aged);
            set_mtime(&root.join("README.md"), aged);

            let ctx = postgres_context_with_root("gcode-freshness-pregate", root);

            // Start clean, then index so code_indexed_projects.last_indexed_at = NOW().
            invalidate_test_project(&ctx);
            full_index(&ctx);

            // Nothing changed: the pre-gate must skip the advisory lock entirely,
            // even while another connection holds it, and report Checked. Without
            // the gate this would force SkippedBusy.
            let holder = hold_project_lock(&ctx);
            let status = ensure_fresh(&ctx, FreshnessScope::Project).expect("freshness status");
            assert_eq!(status, FreshnessStatus::Checked);

            // Touch a tracked file with a future mtime so the gate trips regardless
            // of skew; with the lock still held it reports SkippedBusy, proving it
            // took the lock path rather than skipping.
            set_mtime(
                &lib,
                SystemTime::now() + std::time::Duration::from_secs(3600),
            );
            let status = ensure_fresh(&ctx, FreshnessScope::Project).expect("freshness status");
            assert_eq!(status, FreshnessStatus::SkippedBusy);
            drop(holder);

            invalidate_test_project(&ctx);
        }

        #[test]
        #[serial_test::serial(serial_db)]
        fn symbol_slice_check_uses_stored_byte_range_hash() {
            let tmp = tempfile::tempdir().expect("tempdir");
            let source = b"fn before() {}\nfn target() {}\n";
            std::fs::write(tmp.path().join("lib.rs"), source).expect("write file");
            let ctx = context_for(tmp.path());
            let start = 15;
            let end = source.len();
            let sym = Symbol {
                id: uuid::Uuid::new_v5(&CODE_INDEX_UUID_NAMESPACE, b"sym").to_string(),
                project_id: "proj".to_string(),
                file_path: "lib.rs".to_string(),
                name: "target".to_string(),
                qualified_name: "target".to_string(),
                kind: "function".to_string(),
                language: "rust".to_string(),
                byte_start: start,
                byte_end: end,
                line_start: 2,
                line_end: 2,
                signature: None,
                docstring: None,
                parent_symbol_id: None,
                content_hash: symbol_hash(source, start, end),
                summary: None,
                created_at: String::new(),
                updated_at: String::new(),
            };

            assert!(symbol_slice_is_current(&ctx, &sym));

            std::fs::write(
                tmp.path().join("lib.rs"),
                b"// shifted\nfn before() {}\nfn target() {}\n",
            )
            .expect("shift file");
            assert!(!symbol_slice_is_current(&ctx, &sym));
        }
    }
}

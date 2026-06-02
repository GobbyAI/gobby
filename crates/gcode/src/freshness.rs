use std::path::{Path, PathBuf};

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
            daemon_url: None,
            index_scope: crate::config::ProjectIndexScope::Single,
        }
    }

    fn symbol_hash(source: &[u8], start: usize, end: usize) -> String {
        hasher::symbol_content_hash(source, start, end).expect("symbol hash")
    }

    fn postgres_test_context(project_id: &str) -> Option<Context> {
        let database_url = std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL").ok()?;
        match db::connect_readwrite(&database_url) {
            Ok(_) => Some(Context {
                database_url,
                project_root: std::path::PathBuf::from("/tmp/gcode-freshness-lock-test"),
                project_id: project_id.to_string(),
                quiet: true,
                falkordb: None,
                qdrant: None,
                embedding: None,
                code_vectors: crate::config::CodeVectorSettings::default(),
                daemon_url: None,
                index_scope: crate::config::ProjectIndexScope::Single,
            }),
            Err(error) => {
                eprintln!("skipping freshness lock test: PostgreSQL hub is unavailable: {error}");
                None
            }
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

    #[test]
    #[serial_test::serial]
    fn no_freshness_env_short_circuits_project_refresh() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let ctx = context_for(tmp.path());
        unsafe { std::env::set_var(INFLIGHT_ENV, "1") };
        let result = ensure_fresh(&ctx, FreshnessScope::Project);
        unsafe { std::env::remove_var(INFLIGHT_ENV) };

        assert_eq!(result.expect("freshness status"), FreshnessStatus::Checked);
    }

    #[test]
    #[serial_test::serial]
    fn busy_project_lock_skips_freshness_refresh() {
        let Some(ctx) = postgres_test_context("gcode-freshness-busy") else {
            return;
        };
        let _holder = hold_project_lock(&ctx);

        let status = ensure_fresh(&ctx, FreshnessScope::Project).expect("freshness status");

        assert_eq!(status, FreshnessStatus::SkippedBusy);
    }

    #[test]
    #[serial_test::serial]
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

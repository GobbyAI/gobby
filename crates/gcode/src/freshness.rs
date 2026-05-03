use std::path::{Path, PathBuf};

use crate::config::Context;
use crate::db;
use crate::index::{hasher, indexer};
use crate::models::Symbol;

const INFLIGHT_ENV: &str = "GCODE_FRESHNESS_INFLIGHT";

pub enum FreshnessScope {
    Project,
    Files(Vec<PathBuf>),
}

pub fn ensure_fresh(ctx: &Context, scope: FreshnessScope) -> anyhow::Result<()> {
    if std::env::var_os(INFLIGHT_ENV).is_some() {
        return Ok(());
    }

    let _guard = FreshnessGuard::enter();
    let conn = db::open_readwrite(&ctx.db_path)?;
    match scope {
        FreshnessScope::Project => {
            indexer::index_directory(&conn, &ctx.project_root, &ctx.project_id, true, ctx.quiet)?;
        }
        FreshnessScope::Files(paths) => {
            let files: Vec<String> = paths
                .iter()
                .map(|path| normalize_file_path(&ctx.project_root, path))
                .collect();
            if !files.is_empty() {
                indexer::index_files(&conn, &ctx.project_root, &ctx.project_id, &files)?;
            }
        }
    }
    Ok(())
}

pub fn ensure_symbol_fresh(ctx: &Context, id: &str) -> anyhow::Result<()> {
    if std::env::var_os(INFLIGHT_ENV).is_some() {
        return Ok(());
    }

    let conn = db::open_readonly(&ctx.db_path)?;
    let sym: Option<Symbol> = conn
        .query_row(
            "SELECT * FROM code_symbols WHERE id = ?1 AND project_id = ?2",
            rusqlite::params![id, &ctx.project_id],
            Symbol::from_row,
        )
        .ok();
    drop(conn);

    let Some(sym) = sym else {
        return Ok(());
    };

    if symbol_slice_is_current(ctx, &sym) {
        return Ok(());
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

    fn context_for(root: &Path, db_path: PathBuf) -> Context {
        Context {
            db_path,
            project_root: root.to_path_buf(),
            project_id: "proj".to_string(),
            quiet: true,
            neo4j: None,
            qdrant: None,
            embedding: None,
            daemon_url: None,
        }
    }

    fn symbol_hash(source: &[u8], start: usize, end: usize) -> String {
        hasher::symbol_content_hash(source, start, end).expect("symbol hash")
    }

    #[test]
    #[serial_test::serial]
    fn no_freshness_env_short_circuits_project_refresh() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let ctx = context_for(tmp.path(), tmp.path().join("missing.db"));
        unsafe { std::env::set_var(INFLIGHT_ENV, "1") };
        let result = ensure_fresh(&ctx, FreshnessScope::Project);
        unsafe { std::env::remove_var(INFLIGHT_ENV) };

        assert!(result.is_ok());
        assert!(!ctx.db_path.exists());
    }

    #[test]
    #[serial_test::serial]
    fn symbol_slice_check_uses_stored_byte_range_hash() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let source = b"fn before() {}\nfn target() {}\n";
        std::fs::write(tmp.path().join("lib.rs"), source).expect("write file");
        let ctx = context_for(tmp.path(), tmp.path().join("index.db"));
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

    #[test]
    #[serial_test::serial]
    fn ensure_symbol_fresh_reindexes_shifted_symbol_slice() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let db_path = tmp.path().join("index.db");
        let source = "pub fn target() {\n}\n";
        std::fs::write(tmp.path().join("lib.rs"), source).expect("write file");
        let conn = db::open_readwrite(&db_path).expect("open db");
        indexer::index_directory(&conn, tmp.path(), "proj", true, true).expect("index");
        drop(conn);

        let ctx = context_for(tmp.path(), db_path.clone());
        let conn = db::open_readonly(&db_path).expect("open readonly");
        let old_id: String = conn
            .query_row(
                "SELECT id FROM code_symbols WHERE project_id = 'proj' AND name = 'target'",
                [],
                |row| row.get(0),
            )
            .expect("old symbol id");
        drop(conn);

        std::fs::write(
            tmp.path().join("lib.rs"),
            "// inserted\npub fn target() {\n}\n",
        )
        .expect("shift file");

        ensure_symbol_fresh(&ctx, &old_id).expect("freshen symbol");

        let conn = db::open_readonly(&db_path).expect("open readonly");
        let old_exists: bool = conn
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM code_symbols WHERE id = ?1)",
                rusqlite::params![old_id],
                |row| row.get(0),
            )
            .expect("exists query");
        let new_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM code_symbols WHERE project_id = 'proj' AND name = 'target'",
                [],
                |row| row.get(0),
            )
            .expect("count query");

        assert!(!old_exists);
        assert_eq!(new_count, 1);
    }
}

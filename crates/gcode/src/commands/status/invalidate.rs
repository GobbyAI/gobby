use crate::config::{Context, QdrantConfig};
use crate::db;
use crate::graph::code_graph;
use crate::index::indexer;
use crate::index_lock::{IndexLockPolicy, lock_project_by_id};
use crate::output::Format;
use crate::vector::code_symbols;

pub fn invalidate(ctx: &Context, force: bool, _format: Format) -> anyhow::Result<()> {
    if !force {
        let project_name = ctx
            .project_root
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_else(|| ctx.project_id.clone());

        eprint!(
            "This will clear the entire code index for '{}'. Continue? [y/N] ",
            project_name
        );
        let _ = std::io::Write::flush(&mut std::io::stderr());

        let mut input = String::new();
        std::io::stdin().read_line(&mut input)?;
        if !input.trim().eq_ignore_ascii_case("y") {
            eprintln!("Aborted.");
            return Ok(());
        }
    }

    invalidate_project(ctx)
}

pub(crate) fn invalidate_project(ctx: &Context) -> anyhow::Result<()> {
    let _lock = lock_project_by_id(
        &ctx.database_url,
        &ctx.project_id,
        IndexLockPolicy::maintenance_try(),
    )?
    .ok_or_else(|| {
        anyhow::anyhow!(
            "gcode index lock is busy for project {}; retry invalidation later",
            ctx.project_id
        )
    })?;

    invalidate_project_locked(ctx).map(|_| ())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ProjectInvalidationResult {
    pub(crate) graph_cleared: bool,
    pub(crate) qdrant_deleted: Option<usize>,
}

pub(crate) fn invalidate_project_locked(
    ctx: &Context,
) -> anyhow::Result<ProjectInvalidationResult> {
    let qdrant_deleted = run_projection_first(
        ctx.falkordb.is_some(),
        ctx.qdrant.is_some(),
        || {
            code_graph::clear_project(ctx)
                .map_err(|err| anyhow::anyhow!("failed to clear FalkorDB projection: {err}"))
        },
        || {
            let qdrant = require_configured_qdrant(ctx.qdrant.as_ref())?;
            code_symbols::delete_project_collection(qdrant, &ctx.project_id)
                .map_err(|err| anyhow::anyhow!("failed to delete Qdrant projection: {err}"))
        },
        || {
            let mut conn = db::connect_readwrite(&ctx.database_url)?;
            indexer::invalidate(&mut conn, &ctx.project_id, ctx.daemon_url.as_deref())
        },
    )?;
    Ok(ProjectInvalidationResult {
        graph_cleared: ctx.falkordb.is_some(),
        qdrant_deleted,
    })
}

fn require_configured_qdrant(qdrant: Option<&QdrantConfig>) -> anyhow::Result<&QdrantConfig> {
    qdrant.ok_or_else(|| anyhow::anyhow!("Qdrant cleanup was selected without a configured client"))
}

fn run_projection_first<Falkor, Qdrant, Sql, QdrantOutput>(
    has_falkor_config: bool,
    has_qdrant_config: bool,
    clear_falkor: Falkor,
    clear_qdrant: Qdrant,
    invalidate_sql: Sql,
) -> anyhow::Result<Option<QdrantOutput>>
where
    Falkor: FnOnce() -> anyhow::Result<()>,
    Qdrant: FnOnce() -> anyhow::Result<QdrantOutput>,
    Sql: FnOnce() -> anyhow::Result<()>,
{
    if has_falkor_config {
        clear_falkor()?;
    }
    let qdrant_output = has_qdrant_config.then(clear_qdrant).transpose()?;
    invalidate_sql()?;
    Ok(qdrant_output)
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::path::PathBuf;

    use super::*;

    fn test_context(database_url: String, project_id: &str) -> Context {
        Context {
            database_url,
            project_root: PathBuf::new(),
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

    fn insert_indexed_project(conn: &mut postgres::Client, project_id: &str) {
        let project_id = db::id_param(project_id).expect("test project id is a UUID");
        conn.execute(
            "INSERT INTO code_indexed_projects
                (id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms)
             VALUES ($1, '/missing/root', 0, 0, NOW(), 0)",
            &[&project_id],
        )
        .expect("insert indexed project");
    }

    fn indexed_project_exists(conn: &mut postgres::Client, project_id: &str) -> bool {
        let project_id = db::id_param(project_id).expect("test project id is a UUID");
        conn.query_one(
            "SELECT EXISTS(SELECT 1 FROM code_indexed_projects WHERE id = $1)",
            &[&project_id],
        )
        .expect("query indexed project")
        .get(0)
    }

    fn delete_indexed_project(conn: &mut postgres::Client, project_id: &str) {
        let project_id = db::id_param(project_id).expect("test project id is a UUID");
        conn.execute(
            "DELETE FROM code_indexed_projects WHERE id = $1",
            &[&project_id],
        )
        .expect("delete indexed project");
    }

    #[test]
    fn projection_cleanup_precedes_sql_invalidation() {
        let events = RefCell::new(Vec::new());

        run_projection_first(
            true,
            true,
            || {
                events.borrow_mut().push("falkor");
                Ok(())
            },
            || {
                events.borrow_mut().push("qdrant");
                Ok(())
            },
            || {
                events.borrow_mut().push("sql");
                Ok(())
            },
        )
        .expect("projection-first invalidation succeeds");

        assert_eq!(*events.borrow(), ["falkor", "qdrant", "sql"]);
    }

    #[test]
    fn missing_projection_config_skips_only_that_backend() {
        let cases: &[(bool, bool, &[&str])] = &[
            (false, true, &["qdrant", "sql"]),
            (true, false, &["falkor", "sql"]),
            (false, false, &["sql"]),
        ];

        for &(has_falkor, has_qdrant, expected) in cases {
            let events = RefCell::new(Vec::new());
            run_projection_first(
                has_falkor,
                has_qdrant,
                || {
                    events.borrow_mut().push("falkor");
                    Ok(())
                },
                || {
                    events.borrow_mut().push("qdrant");
                    Ok(())
                },
                || {
                    events.borrow_mut().push("sql");
                    Ok(())
                },
            )
            .expect("missing projection config is an honest skip");

            assert_eq!(*events.borrow(), expected);
        }
    }

    #[test]
    fn configured_falkor_failure_aborts_before_qdrant_and_sql() {
        let events = RefCell::new(Vec::new());

        let error = run_projection_first(
            true,
            true,
            || {
                events.borrow_mut().push("falkor");
                anyhow::bail!("FalkorDB unavailable")
            },
            || {
                events.borrow_mut().push("qdrant");
                Ok(())
            },
            || {
                events.borrow_mut().push("sql");
                Ok(())
            },
        )
        .expect_err("configured FalkorDB failure must abort invalidation");

        assert!(error.to_string().contains("FalkorDB unavailable"));
        assert_eq!(*events.borrow(), ["falkor"]);
    }

    #[test]
    fn configured_qdrant_failure_aborts_before_sql() {
        let events = RefCell::new(Vec::new());

        let error = run_projection_first(
            true,
            true,
            || {
                events.borrow_mut().push("falkor");
                Ok(())
            },
            || -> anyhow::Result<()> {
                events.borrow_mut().push("qdrant");
                anyhow::bail!("Qdrant unavailable")
            },
            || {
                events.borrow_mut().push("sql");
                Ok(())
            },
        )
        .expect_err("configured Qdrant failure must abort SQL invalidation");

        assert!(error.to_string().contains("Qdrant unavailable"));
        assert_eq!(*events.borrow(), ["falkor", "qdrant"]);
    }

    #[test]
    fn configured_qdrant_cleanup_requires_client() {
        let error = require_configured_qdrant(None)
            .expect_err("configured Qdrant cleanup without a client must fail");

        assert!(
            error
                .to_string()
                .contains("selected without a configured client")
        );
    }

    mod serial_db {
        use super::*;

        #[test]
        #[cfg_attr(
            not(gcode_postgres_tests),
            ignore = "requires a PostgreSQL test database URL"
        )]
        #[serial_test::serial(serial_db)]
        fn project_id_invalidation_needs_no_project_root() {
            let database_url = crate::test_env::postgres_test_database_url("invalidate tests");
            let mut conn =
                db::connect_readwrite(&database_url).expect("connect invalidate test DB");
            let project_id = uuid::Uuid::new_v5(
                &uuid::Uuid::NAMESPACE_OID,
                b"gcode-invalidate-rootless-project",
            )
            .to_string();
            delete_indexed_project(&mut conn, &project_id);
            insert_indexed_project(&mut conn, &project_id);

            let ctx = test_context(database_url, &project_id);
            assert!(ctx.project_root.as_os_str().is_empty());
            invalidate_project(&ctx).expect("invalidate project by id without a root");

            assert!(!indexed_project_exists(&mut conn, &project_id));
        }

        #[test]
        #[cfg_attr(
            not(gcode_postgres_tests),
            ignore = "requires a PostgreSQL test database URL"
        )]
        #[serial_test::serial(serial_db)]
        fn busy_project_lock_leaves_sql_discovery_row_untouched() {
            let database_url = crate::test_env::postgres_test_database_url("invalidate tests");
            let mut conn =
                db::connect_readwrite(&database_url).expect("connect invalidate test DB");
            let project_id =
                uuid::Uuid::new_v5(&uuid::Uuid::NAMESPACE_OID, b"gcode-invalidate-busy-project")
                    .to_string();
            delete_indexed_project(&mut conn, &project_id);
            insert_indexed_project(&mut conn, &project_id);

            let mut holder =
                db::connect_readwrite(&database_url).expect("connect advisory lock holder");
            let lock_key = crate::index_lock::project_lock_key(&project_id);
            holder
                .query_one("SELECT pg_advisory_lock($1)", &[&lock_key])
                .expect("hold project advisory lock");

            let mut ctx = test_context(database_url, &project_id);
            ctx.qdrant = Some(crate::config::QdrantConfig {
                url: Some("http://127.0.0.1:1".to_string()),
                api_key: None,
            });
            let error = invalidate_project(&ctx).expect_err("busy project lock must fail visibly");

            assert!(error.to_string().contains("index lock is busy"));
            assert!(indexed_project_exists(&mut conn, &project_id));
            delete_indexed_project(&mut conn, &project_id);
        }
    }
}

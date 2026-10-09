use super::*;
use gobby_core::postgres_pool::PoolSettings;
use tokio_postgres::{Client, NoTls};

const CUTOFF: &str = "2026-04-11T12:00:00Z";
const SESSION: &str = "11111111-1111-4111-8111-111111111111";

struct Fixture {
    admin: Client,
    pool: Pool,
    schema: String,
    database_url: String,
    admin_pid: i32,
}

impl Fixture {
    async fn new() -> Result<Option<Self>> {
        let Ok(database_url) = std::env::var("GOBBY_SCHEMA_TEST_DATABASE_URL") else {
            eprintln!("skipped: GOBBY_SCHEMA_TEST_DATABASE_URL is unset");
            return Ok(None);
        };
        let config: tokio_postgres::Config = database_url.parse()?;
        anyhow::ensure!(
            config.get_dbname().unwrap_or_default().ends_with("_test"),
            "retention tests require an isolated *_test database"
        );
        let (admin, connection) = config.connect(NoTls).await?;
        tokio::spawn(async move {
            let _ = connection.await;
        });
        let admin_pid = admin
            .query_one("SELECT pg_backend_pid()", &[])
            .await?
            .try_get(0)?;
        let schema = format!("gobby_test_retention_{}", uuid::Uuid::new_v4().simple());
        admin
            .batch_execute(&format!(
                "CREATE SCHEMA {schema}; SET search_path TO {schema}; \
             CREATE TABLE sessions (id uuid PRIMARY KEY); \
             INSERT INTO sessions VALUES ('{SESSION}'); \
             CREATE TABLE token_events (id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, \
             session_id uuid NOT NULL REFERENCES sessions(id), event_at timestamptz NOT NULL, \
             input_tokens integer NOT NULL, output_tokens integer NOT NULL, \
             cache_creation_tokens integer NOT NULL, cache_read_tokens integer NOT NULL);"
            ))
            .await?;
        admin
            .batch_execute(include_str!(
                "../../../../gcore/assets/schema/migrations/465_token_event_retention.sql"
            ))
            .await?;
        admin
            .batch_execute(&format!(
                "GRANT USAGE ON SCHEMA {schema} TO gobby_daemon_runtime; \
             GRANT SELECT, UPDATE, DELETE ON token_events TO gobby_daemon_runtime"
            ))
            .await?;
        let separator = if database_url.contains('?') { '&' } else { '?' };
        let scoped = format!("{database_url}{separator}options=-csearch_path%3D{schema}");
        let pool = Pool::build(
            &scoped,
            PoolSettings {
                max_size: 2,
                application_name: "gobby-gdaemon-retention-test".to_owned(),
                acquire_timeout: Duration::from_secs(5),
            },
        )?;
        Ok(Some(Self {
            admin,
            pool,
            schema,
            database_url,
            admin_pid,
        }))
    }

    async fn seed(&self, count: i32) -> Result<()> {
        self.admin.execute(
            "INSERT INTO token_events \
             (session_id,event_at,input_tokens,output_tokens,cache_creation_tokens,cache_read_tokens) \
             SELECT $1::text::uuid, $2::text::timestamptz - INTERVAL '1 day', 1,2,3,4 \
             FROM generate_series(1,$3::int)", &[&SESSION, &CUTOFF, &count]
        ).await?;
        Ok(())
    }

    async fn raw_count(&self) -> Result<i64> {
        Ok(self
            .admin
            .query_one("SELECT count(*) FROM token_events", &[])
            .await?
            .try_get(0)?)
    }

    async fn totals(&self) -> Result<(i64, i64, i64, i64)> {
        let row = self
            .admin
            .query_one(
                "SELECT input_tokens,output_tokens,cache_creation_tokens,cache_read_tokens \
             FROM token_event_retention_totals WHERE session_id=$1::text::uuid",
                &[&SESSION],
            )
            .await?;
        Ok((
            row.try_get(0)?,
            row.try_get(1)?,
            row.try_get(2)?,
            row.try_get(3)?,
        ))
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let url = self.database_url.clone();
        let schema = self.schema.clone();
        let pid = self.admin_pid;
        let _ = std::thread::spawn(move || {
            if let Ok(mut client) = gobby_core::postgres::connect_readwrite(&url) {
                let _ = client.execute("SELECT pg_terminate_backend($1)", &[&pid]);
                let _ = client.batch_execute(&format!("DROP SCHEMA {schema} CASCADE"));
            }
        })
        .join();
    }
}

#[tokio::test]
async fn serial_db_cutoff_is_strict_and_usage_survives_pruning() -> Result<()> {
    let Some(db) = Fixture::new().await? else {
        return Ok(());
    };
    db.admin
        .execute(
            "INSERT INTO token_events \
         (session_id,event_at,input_tokens,output_tokens,cache_creation_tokens,cache_read_tokens) \
         SELECT $1::text::uuid, $2::text::timestamptz + delta, 1,2,3,4 FROM \
         (VALUES (INTERVAL '-1 day'), (INTERVAL '-1 second'), (INTERVAL '0'), \
         (INTERVAL '1 second'), (INTERVAL '1 day')) AS edges(delta)",
            &[&SESSION, &CUTOFF],
        )
        .await?;
    let session = db.pool.dedicated_session().await?;
    assert_eq!(prune_batch(&session, CUTOFF).await?, 2);
    assert_eq!(db.raw_count().await?, 3);
    assert_eq!(db.totals().await?, (2, 4, 6, 8));
    let expired: i64 = db
        .admin
        .query_one(
            "SELECT count(*) FROM token_events WHERE event_at < $1::text::timestamptz",
            &[&CUTOFF],
        )
        .await?
        .try_get(0)?;
    assert_eq!(expired, 0);
    assert_eq!(prune_batch(&session, CUTOFF).await?, 0);
    assert_eq!(db.totals().await?, (2, 4, 6, 8));
    Ok(())
}

#[tokio::test]
async fn serial_db_batches_are_ordered_and_cycle_is_capped() -> Result<()> {
    let Some(db) = Fixture::new().await? else {
        return Ok(());
    };
    db.seed(200_001).await?;
    db.admin
        .execute(
            "UPDATE token_events SET event_at = event_at - INTERVAL '1 day' WHERE id=200001",
            &[],
        )
        .await?;
    let session = db.pool.dedicated_session().await?;
    assert_eq!(prune_batch(&session, CUTOFF).await?, 10_000);
    let first_remaining: i32 = db
        .admin
        .query_one("SELECT min(id) FROM token_events", &[])
        .await?
        .try_get(0)?;
    assert_eq!(first_remaining, 10_000);
    let last_present: bool = db
        .admin
        .query_one(
            "SELECT EXISTS(SELECT 1 FROM token_events WHERE id=200001)",
            &[],
        )
        .await?
        .try_get(0)?;
    assert!(
        !last_present,
        "oldest event must be pruned before smaller IDs"
    );
    // Restore a full 200001-row backlog to exercise the production cycle cap.
    db.seed(10_000).await?;
    let (_sender, mut stop) = watch::channel(false);
    let report = cycle_at(&session, CUTOFF, &mut stop).await?;
    assert_eq!(
        report,
        CycleReport {
            owner: true,
            deleted: 200_000,
            batches: 20
        }
    );
    assert_eq!(db.raw_count().await?, 1);
    assert_eq!(db.totals().await?, (210_000, 420_000, 630_000, 840_000));
    Ok(())
}

#[tokio::test]
async fn serial_db_archive_failure_rolls_back_delete_and_retry_accumulates() -> Result<()> {
    let Some(db) = Fixture::new().await? else {
        return Ok(());
    };
    db.seed(2).await?;
    db.admin.batch_execute(
        "ALTER TABLE token_event_retention_totals ADD CONSTRAINT reject_archive CHECK (input_tokens < 2)"
    ).await?;
    let session = db.pool.dedicated_session().await?;
    let error = prune_batch(&session, CUTOFF)
        .await
        .expect_err("archive constraint must fail");
    assert_eq!(
        error
            .downcast_ref::<tokio_postgres::Error>()
            .and_then(|e| e.code()),
        Some(&tokio_postgres::error::SqlState::CHECK_VIOLATION)
    );
    assert_eq!(db.raw_count().await?, 2);
    let archives: i64 = db
        .admin
        .query_one("SELECT count(*) FROM token_event_retention_totals", &[])
        .await?
        .try_get(0)?;
    assert_eq!(archives, 0);
    db.admin
        .batch_execute("ALTER TABLE token_event_retention_totals DROP CONSTRAINT reject_archive")
        .await?;
    assert_eq!(prune_batch(&session, CUTOFF).await?, 2);
    db.seed(1).await?;
    assert_eq!(prune_batch(&session, CUTOFF).await?, 1);
    assert_eq!(db.totals().await?, (3, 6, 9, 12));
    Ok(())
}

#[tokio::test]
async fn serial_db_shutdown_finishes_current_transaction_then_stops() -> Result<()> {
    let Some(db) = Fixture::new().await? else {
        return Ok(());
    };
    db.seed(10_001).await?;
    db.admin
        .batch_execute("BEGIN; SELECT id FROM token_events WHERE id=1 FOR UPDATE")
        .await?;
    let session = db.pool.dedicated_session().await?;
    let (sender, mut stop) = watch::channel(false);
    let cycle = tokio::spawn(async move { cycle_at(&session, CUTOFF, &mut stop).await });
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let blocked: bool = db
                .admin
                .query_one(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity \
                 WHERE application_name='gobby-gdaemon-retention-test' AND wait_event_type='Lock')",
                    &[],
                )
                .await?
                .try_get(0)?;
            if blocked {
                return Ok::<_, anyhow::Error>(());
            }
            tokio::task::yield_now().await;
        }
    })
    .await??;
    sender.send_replace(true);
    assert!(
        !cycle.is_finished(),
        "shutdown must not cancel a running transaction"
    );
    db.admin.batch_execute("COMMIT").await?;
    let report = tokio::time::timeout(Duration::from_secs(5), cycle).await???;
    assert_eq!(
        report,
        CycleReport {
            owner: true,
            deleted: 10_000,
            batches: 1
        }
    );
    assert_eq!(db.raw_count().await?, 1);
    assert_eq!(db.totals().await?, (10_000, 20_000, 30_000, 40_000));
    Ok(())
}

#[tokio::test]
async fn serial_db_owner_contention_empty_cycle_and_drop_release() -> Result<()> {
    let Some(db) = Fixture::new().await? else {
        return Ok(());
    };
    let owner = db.pool.dedicated_session().await?;
    let other = db.pool.dedicated_session().await?;
    assert!(owner.try_session_lock(OWNER_LOCK).await?);
    let (_sender, mut stop) = watch::channel(false);
    assert_eq!(
        cycle_at(&other, CUTOFF, &mut stop).await?,
        CycleReport::default()
    );
    drop(owner);
    // The lock owner closes its backend on drop; wait for that bounded close.
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let report = cycle_at(&other, CUTOFF, &mut stop).await?;
            if report.owner {
                assert_eq!(report.deleted, 0);
                assert_eq!(report.batches, 0);
                return Ok::<_, anyhow::Error>(());
            }
            tokio::task::yield_now().await;
        }
    })
    .await??;
    Ok(())
}

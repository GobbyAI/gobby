#![allow(dead_code)] // Each test binary uses a different subset.

//! Isolated test-hub schema and terminal row fixture shared by DB-backed tests.

use std::time::Duration;

use chrono::{DateTime, Utc};
use gobby_core::postgres_pool::{Pool, PoolSettings};
use gobby_terminals::host::PostgresEpochAuthority;
use gobby_terminals::repository::{self, Backend, Ownership, State, Terminal};
use serde_json::json;
use uuid::Uuid;

pub struct Hub {
    url: String,
    schema: String,
    pub pool: Pool,
}

impl Hub {
    pub async fn new() -> anyhow::Result<Self> {
        let url = std::env::var("GOBBY_TERMINALS_TEST_DATABASE_URL")?;
        anyhow::ensure!(
            url == "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test",
            "reconciliation tests require the explicitly selected isolated test hub"
        );
        let schema = format!("gobby_terminal_reconcile_{}", Uuid::new_v4().simple());
        let admin_url = url.clone();
        let owned_schema = schema.clone();
        tokio::task::spawn_blocking(move || {
            let baseline = include_str!("../../../gcore/assets/schema/baseline.sql");
            let terminal_ddl = baseline
                .split_once("CREATE TABLE terminals (")
                .expect("canonical terminal table")
                .1
                .split_once("\n);")
                .expect("terminal table end")
                .0;
            let mut admin = gobby_core::postgres::connect_readwrite(&admin_url)?;
            admin.batch_execute(&format!(
                "SET statement_timeout = 5000;
                 CREATE SCHEMA {owned_schema};
                 CREATE TABLE {owned_schema}.terminals ({terminal_ddl}\n);
                 ALTER TABLE {owned_schema}.terminals ADD PRIMARY KEY (id);
                 CREATE UNIQUE INDEX ON {owned_schema}.terminals (spawn_key);
                 CREATE UNIQUE INDEX ON {owned_schema}.terminals (locator_key)
                    WHERE locator_key IS NOT NULL AND state IN ('pending','live');
                 CREATE TABLE {owned_schema}.reconcile_faults(stage text PRIMARY KEY);
                 CREATE FUNCTION {owned_schema}.inject_terminal_fault() RETURNS trigger
                    LANGUAGE plpgsql AS $fault$
                    BEGIN
                      IF EXISTS(SELECT 1 FROM {owned_schema}.reconcile_faults
                                WHERE stage=TG_ARGV[0]) THEN
                        RAISE EXCEPTION 'injected % failure', TG_ARGV[0];
                      END IF;
                      RETURN NEW;
                    END; $fault$;
                 CREATE TRIGGER fail_terminal_write BEFORE UPDATE ON {owned_schema}.terminals
                    FOR EACH ROW EXECUTE FUNCTION {owned_schema}.inject_terminal_fault('terminal_write');
                 CREATE CONSTRAINT TRIGGER fail_terminal_commit
                    AFTER UPDATE ON {owned_schema}.terminals DEFERRABLE INITIALLY DEFERRED
                    FOR EACH ROW EXECUTE FUNCTION {owned_schema}.inject_terminal_fault('commit');
                 CREATE TABLE {owned_schema}.agent_runs (
                    id uuid PRIMARY KEY, terminal_id uuid, machine_id uuid NOT NULL,
                    status text NOT NULL, pid integer, terminal_reason text,
                    completed_at timestamptz, updated_at timestamptz DEFAULT now(),
                    pending_terminal_action text, pending_terminal_reason text,
                    termination_requested_at timestamptz);
                 GRANT USAGE ON SCHEMA {owned_schema} TO gobby_daemon_runtime;
                 GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {owned_schema}
                    TO gobby_daemon_runtime;"
            ))?;
            let epochs = include_str!("../../../gcore/assets/schema/migrations/468_terminal_host_epochs.sql");
            admin.batch_execute(&epochs.replace("public.", &format!("{owned_schema}.")))?;
            anyhow::Ok(())
        })
        .await??;
        let pool = Pool::build(
            &format!("{url}?options=-c%20search_path%3D{schema}"),
            PoolSettings {
                max_size: 2,
                application_name: "gobby-gdaemon-terminal-reconcile-test".into(),
                acquire_timeout: Duration::from_secs(5),
            },
        )?;
        Ok(Self { url, schema, pool })
    }

    pub fn epoch_authority(&self) -> anyhow::Result<PostgresEpochAuthority> {
        let pool = Pool::build(
            &format!("{}?options=-c%20search_path%3D{}", self.url, self.schema),
            PoolSettings {
                max_size: 2,
                application_name: "gobby-gdaemon-terminal-epoch-test".into(),
                acquire_timeout: Duration::from_secs(5),
            },
        )?;
        Ok(PostgresEpochAuthority::new(pool))
    }

    pub async fn insert(&self, terminal: &Terminal) -> anyhow::Result<()> {
        self.pool
            .transaction(None, async |tx| {
                repository::insert(tx, terminal).await?;
                anyhow::Ok(())
            })
            .await
    }

    pub async fn get(&self, id: Uuid) -> anyhow::Result<Terminal> {
        self.pool
            .transaction(None, async |tx| {
                Ok(repository::get(tx, id).await?.expect("stored terminal"))
            })
            .await
    }

    pub async fn fault(&self, stage: Option<&str>) -> anyhow::Result<()> {
        self.pool
            .transaction(None, async |tx| {
                tx.execute("DELETE FROM reconcile_faults", &[]).await?;
                if let Some(stage) = stage {
                    tx.execute("INSERT INTO reconcile_faults(stage) VALUES($1)", &[&stage])
                        .await?;
                }
                anyhow::Ok(())
            })
            .await
    }
}

impl Drop for Hub {
    fn drop(&mut self) {
        let url = self.url.clone();
        let schema = self.schema.clone();
        let cleanup = std::thread::spawn(move || {
            let mut admin = gobby_core::postgres::connect_readwrite(&url)?;
            admin.batch_execute(&format!(
                "SET statement_timeout = 5000; DROP SCHEMA IF EXISTS {schema} CASCADE"
            ))?;
            anyhow::Ok(())
        })
        .join();
        if !std::thread::panicking() {
            cleanup
                .expect("schema cleanup thread")
                .expect("schema cleanup");
        }
    }
}

pub fn terminal() -> Terminal {
    let now: DateTime<Utc> = "2026-10-01T12:00:00Z".parse().expect("fixture timestamp");
    Terminal {
        id: Uuid::new_v4(),
        backend: Backend::Native,
        ownership: Ownership::Gobby,
        state: State::Pending,
        spawn_key: Some(Uuid::new_v4().to_string()),
        machine_id: Uuid::new_v4(),
        project_id: Uuid::new_v4(),
        session_id: Some(Uuid::new_v4()),
        agent_run_id: Some(Uuid::new_v4()),
        locator: None,
        locator_key: None,
        session_name: Some("agent-pane".into()),
        window_id: Some("@4".into()),
        title: Some("Repository reconciliation".into()),
        host_epoch: None,
        rows: Some(40),
        cols: Some(120),
        attempt_generation: 3,
        attempt_started_at: now,
        process: Some(json!({"pgid": 1234, "start_time": 123.25, "attempt_owner": "retained"})),
        unresolved_writes: json!({"action-1": {"epoch": "E", "seq": 7}}),
        automatic_write_quarantined_at: Some(now),
        automatic_write_quarantine_action_key: Some("action-1".into()),
        created_at: now,
        updated_at: now,
        liveness_at: Some(now),
    }
}

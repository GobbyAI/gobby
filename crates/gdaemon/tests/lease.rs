use anyhow::{Result, ensure};
use futures_util::FutureExt;
use gobby_daemon::lease::{ActiveDaemonLease, LeaseMode, LeaseState};
use std::time::Duration;
use tokio::time::timeout;
use tokio_postgres::{Client, NoTls};
use uuid::Uuid;

struct TestDb {
    admin: Client,
    url: String,
    schema: String,
}

impl TestDb {
    fn lease(&self, token: &str) -> Result<ActiveDaemonLease> {
        ActiveDaemonLease::new(
            &self.url,
            &Uuid::new_v4().to_string(),
            token,
            LeaseMode::Hub,
        )
    }

    async fn runtime_row(&self, token: &str) -> Result<Option<(i64, String)>> {
        self.admin
            .query_opt(
                &format!(
                    "SELECT fencing_epoch, grant_signing_secret FROM {}.deployment_runtime \
                         WHERE deployment_token = $1",
                    self.schema
                ),
                &[&token],
            )
            .await?
            .map(|row| Ok((row.try_get(0)?, row.try_get(1)?)))
            .transpose()
    }
}

// Each nextest case is a separate process. A second advisory namespace serializes
// these tests across processes while the production lock stays database-wide.
async fn database_case(case: impl AsyncFnOnce(&TestDb) -> Result<()>) -> Result<()> {
    let Ok(url) = std::env::var("DATABASE_URL") else {
        eprintln!("DATABASE_URL unset; skipping isolated PostgreSQL lease test");
        return Ok(());
    };
    let config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("gobby_test")
            && config.get_hosts() == [tokio_postgres::config::Host::Tcp("127.0.0.1".into())]
            && config.get_ports() == [60892]
            && std::env::var("GOBBY_TEST_PROTECT").as_deref() == Ok("1"),
        "lease tests require the protected isolated test hub"
    );
    let (admin, connection) = config.connect(NoTls).await?;
    let driver = tokio::spawn(connection);
    timeout(
        Duration::from_secs(30),
        admin.simple_query("SELECT pg_advisory_lock(hashtext('gdaemon-lease-tests'), 1)"),
    )
    .await??;
    let schema = format!("test_rust_lease_{}", Uuid::new_v4().simple());
    admin
        .batch_execute(&format!(
            "CREATE SCHEMA {schema}; GRANT USAGE ON SCHEMA {schema} TO gobby_daemon_runtime; \
         CREATE TABLE {schema}.deployment_runtime (deployment_token text PRIMARY KEY, \
         fencing_epoch bigint NOT NULL, grant_signing_secret text NOT NULL, \
         epoch_updated_at timestamptz NOT NULL)"
        ))
        .await?;
    // The generated schema identifier is safe to encode in the URL. Production
    // gcore parses the connection options and enforces its TLS contract.
    let separator = if url.contains('?') { '&' } else { '?' };
    let db = TestDb {
        admin,
        url: format!("{url}{separator}options=-csearch_path%3D{schema}"),
        schema,
    };
    let outcome = std::panic::AssertUnwindSafe(case(&db)).catch_unwind().await;
    // In-flight test transactions must not hold locks against schema cleanup.
    db.admin.batch_execute("ROLLBACK").await?;
    let cleanup = db
        .admin
        .batch_execute(&format!("DROP SCHEMA {} CASCADE", db.schema))
        .await;
    drop(db);
    driver.abort();
    let _ = driver.await;
    match outcome {
        Ok(result) => {
            cleanup?;
            result
        }
        Err(panic) => {
            if let Err(error) = cleanup {
                eprintln!("lease test cleanup failed: {error}");
            }
            std::panic::resume_unwind(panic)
        }
    }
}

#[tokio::test]
async fn second_daemon_enters_standby() -> Result<()> {
    database_case(async |db| {
        let mut first = db.lease("first")?;
        let mut same = db.lease("first")?;
        let mut other = db.lease("other")?;
        assert!(first.try_acquire().await?);
        let original = db.runtime_row("first").await?.expect("owner row");
        assert_eq!(original.0, 1);
        assert!(!same.try_acquire().await?);
        assert!(!other.try_acquire().await?);
        assert_eq!(db.runtime_row("first").await?, Some(original));
        assert!(db.runtime_row("other").await?.is_none());
        assert!(same.fence().is_none());
        assert_eq!(*same.subscribe().borrow(), LeaseState::Standby);
        first.release().await?;
        assert!(other.try_acquire().await?);
        assert_eq!(other.fence().expect("successor fence").epoch, 1);
        other.release().await
    })
    .await
}

#[tokio::test]
async fn epoch_and_secret_rotate_together() -> Result<()> {
    database_case(async |db| {
        let mut owner = db.lease("rotation")?;
        assert!(owner.try_acquire().await?);
        let old = db.runtime_row("rotation").await?.expect("first fence");
        assert_eq!(old.1.len(), 64);
        owner.heartbeat().await?;
        assert_eq!(*owner.subscribe().borrow(), LeaseState::Active);
        assert!(owner.try_acquire().await?);
        assert_eq!(db.runtime_row("rotation").await?, Some(old.clone()));
        owner.release().await?;
        assert!(owner.fence().is_none());
        assert_eq!(*owner.subscribe().borrow(), LeaseState::Standby);
        assert!(owner.try_acquire().await?);
        let new = db.runtime_row("rotation").await?.expect("next fence");
        assert_eq!(new.0, old.0 + 1);
        assert_ne!(new.1, old.1);
        assert_eq!(owner.fence().expect("new authority").signing_secret, new.1);
        owner.release().await
    })
    .await
}

#[tokio::test]
async fn lost_connection_reenters_standby() -> Result<()> {
    database_case(async |db| {
        let mut owner = db.lease("loss")?;
        let mut successor = db.lease("loss")?;
        let mut state = owner.subscribe();
        assert!(owner.try_acquire().await?);
        state.borrow_and_update();
        let pid = successor.owner().await?.expect("active owner").pid;
        let killed: bool = db
            .admin
            .query_one("SELECT pg_terminate_backend($1)", &[&pid])
            .await?
            .get(0);
        assert!(killed);
        assert!(owner.heartbeat().await.is_err());
        state.changed().await?;
        assert_eq!(*state.borrow(), LeaseState::Lost);
        assert!(owner.fence().is_none());
        assert!(successor.try_acquire().await?);
        assert_eq!(successor.fence().expect("successor").epoch, 2);
        successor.release().await
    })
    .await
}

#[tokio::test]
async fn recovery_refuses_fresh_and_unverified_owners() -> Result<()> {
    database_case(async |db| {
        let mut owner = db.lease("recovery")?;
        let standby = db.lease("recovery")?;
        assert!(owner.try_acquire().await?);
        assert!(
            standby
                .recover_stale_owner(Duration::from_secs(3600))
                .await
                .unwrap_err()
                .to_string()
                .contains("still fresh")
        );
        owner.heartbeat().await?;
        owner.release().await?;
        db.admin
            .batch_execute(
                "SET application_name = 'unknown-owner'; \
            SELECT pg_advisory_lock(hashtext('gobby-single-active-daemon-v1'), \
            hashtext(current_database()))",
            )
            .await?;
        assert!(
            standby
                .recover_stale_owner(Duration::ZERO)
                .await
                .unwrap_err()
                .to_string()
                .contains("unrecognized")
        );
        assert_eq!(
            standby
                .owner()
                .await?
                .expect("unverified remains")
                .application_name,
            "unknown-owner"
        );
        db.admin
            .batch_execute(
                "SELECT pg_advisory_unlock(hashtext('gobby-single-active-daemon-v1'), \
            hashtext(current_database()))",
            )
            .await?;
        Ok(())
    })
    .await
}

#[tokio::test]
async fn verified_stale_recovery_does_not_promote() -> Result<()> {
    database_case(async |db| {
        let mut owner = db.lease("stale")?;
        let mut standby = db.lease("stale")?;
        assert!(owner.try_acquire().await?);
        let recovered = standby.recover_stale_owner(Duration::ZERO).await?;
        assert_eq!(recovered.application_name, owner.application_name());
        assert!(standby.fence().is_none());
        assert!(standby.owner().await?.is_none());
        assert!(owner.heartbeat().await.is_err());
        assert!(standby.try_acquire().await?);
        assert_eq!(standby.fence().expect("promoted").epoch, 2);
        standby.release().await
    })
    .await
}

#[tokio::test]
async fn failed_transaction_and_drop_release_the_dedicated_session() -> Result<()> {
    database_case(async |db| {
        let mut owner = db.lease("failed")?;
        db.admin.batch_execute(&format!("ALTER TABLE {}.deployment_runtime ADD CHECK (fencing_epoch < 0)", db.schema)).await?;
        assert!(owner.try_acquire().await.is_err());
        assert!(owner.fence().is_none());
        assert!(owner.owner().await?.is_none());
        assert!(db.runtime_row("failed").await?.is_none());
        db.admin.batch_execute(&format!("ALTER TABLE {}.deployment_runtime DROP CONSTRAINT deployment_runtime_fencing_epoch_check", db.schema)).await?;
        assert!(owner.try_acquire().await?);
        drop(owner);
        let mut successor = db.lease("failed")?;
        timeout(Duration::from_secs(5), async {
            while !successor.try_acquire().await? {
                tokio::task::yield_now().await;
            }
            anyhow::Ok(())
        }).await??;
        assert_eq!(successor.fence().expect("drop successor").epoch, 2);
        successor.release().await
    }).await
}

#[test]
fn node_mode_is_refused_before_connecting() {
    let node = ActiveDaemonLease::new("invalid url", "machine", "token", LeaseMode::Node);
    assert!(
        node.err()
            .expect("node refused")
            .to_string()
            .contains("node mode")
    );
}

#[tokio::test]
async fn blocked_acquisition_is_bounded_and_cancellation_releases_the_lock() -> Result<()> {
    database_case(async |db| {
        let mut owner = db.lease("blocked")?;
        let mut successor = db.lease("blocked")?;
        db.admin.batch_execute(&format!(
            "BEGIN; LOCK TABLE {}.deployment_runtime IN ACCESS EXCLUSIVE MODE", db.schema
        )).await?;
        let mut acquire = Box::pin(owner.try_acquire());
        let observed = timeout(Duration::from_secs(4), async {
            loop {
                if successor.owner().await?.is_some() {
                    return anyhow::Ok(());
                }
                tokio::task::yield_now().await;
            }
        });
        tokio::select! {
            result = &mut acquire => panic!("blocked acquisition completed early: {}", result.is_ok()),
            result = observed => result??,
        }
        drop(acquire);
        assert!(owner.fence().is_none());
        timeout(Duration::from_secs(7), async {
            while successor.owner().await?.is_some() {
                tokio::task::yield_now().await;
            }
            anyhow::Ok(())
        }).await??;
        // A second blocked acquisition reaches the five-second operation bound.
        let result = timeout(Duration::from_secs(7), owner.try_acquire()).await?;
        assert!(result.is_err());
        db.admin.batch_execute("ROLLBACK").await?;
        assert!(db.runtime_row("blocked").await?.is_none());
        assert!(successor.try_acquire().await?);
        assert_eq!(successor.fence().expect("successor fence").epoch, 1);
        successor.release().await
    }).await
}

use anyhow::{Context, Result, ensure};
use futures_util::FutureExt;
use gobby_core::postgres_pool::{Pool, PoolSettings};
use gobby_daemon::lease::{ActiveDaemonLease, LeaseMode, LeaseState};
use std::sync::Arc;
use std::time::Duration;
use tokio::time::timeout;
use tokio_postgres::{Client, NoTls};
use uuid::Uuid;

#[path = "common/hub_lock.rs"]
pub(super) mod hub_lock;

struct TestDb {
    admin: Client,
    pool: Arc<Pool>,
    schema: String,
}

fn pool(url: &str) -> Result<Arc<Pool>> {
    Ok(Arc::new(Pool::build(
        url,
        PoolSettings {
            max_size: 8,
            application_name: "gobby-gdaemon-lease-test".into(),
            acquire_timeout: Duration::from_secs(5),
        },
    )?))
}

#[cfg(unix)]
#[tokio::test]
async fn native_standby_control_is_authenticated_and_other_routes_refuse() -> Result<()> {
    use axum::{
        body::{Body, to_bytes},
        http::{Request, StatusCode},
    };
    use gobby_daemon::lease::NamedLock;
    use gobby_daemon::{
        front_door::{
            FrontDoor, FrontDoorState,
            auth::{AuthState, KeyIdentity, KeyResolver, ResolveFuture},
            health::BackendState,
            routes::{FAMILIES, RouteTable},
        },
        lifecycle::backend::BackendStatus,
        state::RuntimeState,
    };
    use std::{collections::BTreeMap, sync::Arc};
    use tokio::sync::watch;

    struct StandbyKeyResolver;
    impl KeyResolver for StandbyKeyResolver {
        fn resolve<'a>(&'a self, key_hash: &'a str) -> ResolveFuture<'a> {
            assert_eq!(key_hash, gobby_core::api_key_format::hash(API_KEY));
            Box::pin(async {
                Ok(Some(KeyIdentity {
                    user_id: "operator".into(),
                    machine_id: "local".into(),
                    key_id: "standby-key".into(),
                }))
            })
        }
    }
    const API_KEY: &str = "gobby_003aUlTJC7tjlCTQj2uNU3MFagCXG9LRKRcwGkBIDlf1Yo7hP";

    database_case(async |db| {
        let token = Uuid::new_v4().to_string();
        let mut owner = db.lease(&token)?;
        assert!(owner.try_acquire().await?);
        let standby = db.lease(&token)?;
        let lease_state = Some(standby.subscribe());
        let standby = Arc::new(NamedLock::new(standby));
        let (backend, rx) = watch::channel(BackendStatus::Standby);
        let runtime = Arc::new(RuntimeState {
            mode: LeaseMode::Hub,
            lease: Some(standby),
            lease_state,
            backend: rx,
            backend_wait: watch::channel(None).1,
            write_token: Default::default(),
            terminal_route: Default::default(),
        });
        let home = tempfile::tempdir()?;
        let bootstrap = home.path().join("bootstrap.yaml");
        let peer = "127.0.0.1:12345".parse()?;
        let mut state = FrontDoorState::new(
            peer,
            BackendState::Starting,
            Arc::new(AuthState::new(
                "test-secret".into(),
                Some(Arc::new(StandbyKeyResolver)),
                bootstrap,
            )?),
        );
        state.runtime = Some(runtime.clone());
        let table = RouteTable::new(FAMILIES, &BTreeMap::new(), &state);
        let door = FrontDoor::new(state, table);

        // Slow lease I/O must not stall health or other standby refusals.
        let guard = runtime
            .lease
            .as_ref()
            .expect("standby lease")
            .lock_as("front door test")
            .await;
        let response = tokio::time::timeout(
            std::time::Duration::from_millis(100),
            door.handle(
                Request::builder().uri("/api/health").body(Body::empty())?,
                peer,
                false,
            ),
        )
        .await?;
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        drop(guard);

        for (status, error) in [
            (BackendStatus::Standby, "standby"),
            (
                BackendStatus::Refused {
                    reason: "schema mismatch".into(),
                },
                "backend_refused",
            ),
        ] {
            backend.send_replace(status);
            for path in [
                "/api/health",
                "/api/admin/status",
                "/",
                "/api/admin/restart",
                "/ws",
            ] {
                let response = door
                    .handle(
                        Request::builder().uri(path).body(Body::empty())?,
                        peer,
                        false,
                    )
                    .await;
                assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE, "{path}");
                let body: serde_json::Value =
                    serde_json::from_slice(&to_bytes(response.into_body(), 65536).await?)?;
                assert_eq!(body["error"], error);
            }
            for (method, path) in [
                ("GET", "/api/admin/lease/status"),
                ("POST", "/api/admin/lease/promote"),
                ("POST", "/api/admin/lease/recover"),
            ] {
                let request = Request::builder()
                    .method(method)
                    .uri(path)
                    .header("x-gobby-key-id", "forged-key")
                    .header("x-gobby-front-door", "test-secret")
                    .body(Body::empty())?;
                assert_eq!(
                    door.handle(request, peer, false).await.status(),
                    StatusCode::UNAUTHORIZED
                );
            }
        }
        backend.send_replace(BackendStatus::Standby);
        let request = Request::builder()
            .uri("/api/admin/lease/status")
            .header("authorization", format!("Bearer {API_KEY}"))
            .body(Body::empty())?;
        let response = door.handle(request, peer, false).await;
        assert_eq!(response.status(), StatusCode::OK);
        let body: serde_json::Value =
            serde_json::from_slice(&to_bytes(response.into_body(), 65536).await?)?;
        assert_eq!(body["mode"], "standby");
        assert_eq!(body["held"], true);
        let request = Request::builder()
            .method("POST")
            .uri("/api/admin/lease/promote")
            .header("authorization", format!("Bearer {API_KEY}"))
            .body(Body::empty())?;
        let response = door.handle(request, peer, false).await;
        assert_eq!(response.status(), StatusCode::CONFLICT);
        let body: serde_json::Value =
            serde_json::from_slice(&to_bytes(response.into_body(), 65536).await?)?;
        assert_eq!(body["detail"]["message"], "active-daemon lease is held");
        owner.release().await?;
        let request = Request::builder()
            .method("POST")
            .uri("/api/admin/lease/promote")
            .header("authorization", format!("Bearer {API_KEY}"))
            .body(Body::empty())?;
        let response = door.handle(request, peer, false).await;
        assert_eq!(response.status(), StatusCode::OK);
        let body: serde_json::Value =
            serde_json::from_slice(&to_bytes(response.into_body(), 65536).await?)?;
        assert_eq!(body, serde_json::json!({"promoting": true}));
        Ok(())
    })
    .await
}

impl TestDb {
    fn lease(&self, token: &str) -> Result<ActiveDaemonLease> {
        ActiveDaemonLease::new(
            self.pool.clone(),
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

/// Lease and lifecycle fixtures in every worktree share this key on the one test hub.
/// A timed-out wait is cancelled server-side, because a backend waiting on a lock never
/// notices its client closing and would stay queued ahead of later runs. The error then
/// names each holder; fixture sessions carry their host process ID in application_name.
pub(crate) async fn lock_fixture_key(admin: &Client, bound: Duration) -> Result<()> {
    let wait = admin.simple_query("SELECT pg_advisory_lock(hashtext('gdaemon-lease-tests'), 1)");
    tokio::pin!(wait);
    if let Ok(locked) = timeout(bound, &mut wait).await {
        locked?;
        return Ok(());
    }
    admin.cancel_token().cancel_query(NoTls).await?;
    // Await the wait's own outcome so the cancel cannot land on the next statement.
    if wait.await.is_ok() {
        return Ok(());
    }
    let holders = admin
        .query(
            "SELECT a.pid, a.application_name, a.backend_start::text, a.state, \
             left(a.query, 80) FROM pg_locks l JOIN pg_stat_activity a USING (pid) \
             WHERE l.locktype = 'advisory' AND l.granted AND l.objid = 1 AND l.objsubid = 2 \
             AND l.classid = hashtext('gdaemon-lease-tests')::oid \
             AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())",
            &[],
        )
        .await?
        .iter()
        .map(|row| {
            Ok(format!(
                "pid={} application_name={:?} backend_start={} state={:?} query={:?}",
                row.try_get::<_, i32>(0)?,
                row.try_get::<_, Option<String>>(1)?.unwrap_or_default(),
                row.try_get::<_, Option<String>>(2)?.unwrap_or_default(),
                row.try_get::<_, Option<String>>(3)?.unwrap_or_default(),
                row.try_get::<_, Option<String>>(4)?.unwrap_or_default(),
            ))
        })
        .collect::<Result<Vec<_>>>()?;
    anyhow::bail!(
        "lease fixture key still held after {bound:?}; holders: [{}]",
        holders.join("; ")
    )
}

#[tokio::test]
async fn fixture_key_timeout_names_the_holder_and_leaves_no_waiter() -> Result<()> {
    database_case(async |db| {
        // database_case holds the key on db.admin, so a second session must time out.
        let holder: i32 = db
            .admin
            .query_one("SELECT pg_backend_pid()", &[])
            .await?
            .try_get(0)?;
        let url = std::env::var("DATABASE_URL")?;
        let (waiter, connection) = url
            .parse::<tokio_postgres::Config>()?
            .connect(NoTls)
            .await?;
        let driver = tokio::spawn(connection);
        let waiter_pid: i32 = waiter
            .query_one("SELECT pg_backend_pid()", &[])
            .await?
            .try_get(0)?;
        let error = lock_fixture_key(&waiter, Duration::from_millis(200))
            .await
            .expect_err("the key is held by this case's fixture");
        let message = format!("{error:#}");
        assert!(message.contains(&format!("pid={holder} ")), "{message}");
        let fixture = format!("gdaemon-lease-fixture:{}", std::process::id());
        assert!(message.contains(&fixture), "{message}");
        // A backend waiting on a lock never notices its client closing, so a timed-out
        // wait must be cancelled or it stays queued ahead of every later run.
        let queued: bool = db
            .admin
            .query_one(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = $1 AND NOT granted)",
                &[&waiter_pid],
            )
            .await?
            .try_get(0)?;
        assert!(!queued, "timed-out waiter {waiter_pid} is still queued");
        driver.abort();
        Ok(())
    })
    .await
}

// Each nextest case is a separate process. A second advisory namespace serializes
// these tests across processes while the production lock stays database-wide.
async fn database_case(case: impl AsyncFnOnce(&TestDb) -> Result<()>) -> Result<()> {
    let Ok(url) = std::env::var("DATABASE_URL") else {
        eprintln!("DATABASE_URL unset; skipping isolated PostgreSQL lease test");
        return Ok(());
    };
    let mut config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("gobby_test")
            && config.get_hosts() == [tokio_postgres::config::Host::Tcp("127.0.0.1".into())]
            && config.get_ports() == [60892]
            && std::env::var("GOBBY_TEST_PROTECT").as_deref() == Ok("1"),
        "lease tests require the protected isolated test hub"
    );
    config.application_name(format!("gdaemon-lease-fixture:{}", std::process::id()));
    let (admin, connection) = config.connect(NoTls).await?;
    let driver = tokio::spawn(connection);
    // The lock is shared with every worktree's lease and lifecycle runs on this hub, and a
    // waiter queues behind whole test bodies from those runs. The bound must outlast several
    // of them, so it is a hang guard, not a fairness budget.
    lock_fixture_key(&admin, Duration::from_secs(300)).await?;
    // Armed only after the lock is held, so a waiter's own timeout still covers the queue.
    let _watchdog = hub_lock::arm(hub_lock::HOLD_DEADLINE);
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
        pool: pool(&format!("{url}{separator}options=-csearch_path%3D{schema}"))?,
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

/// The body of the 409 that refuses an admin promote or recover, and the error of an
/// acquisition, while this daemon's own lease lifetime is open.
pub(crate) const LIFETIME_OPEN: &str = "this daemon's lease lifetime has not ended";

/// Ends a lease's dedicated session by its PID, as `lost_connection_reenters_standby` does.
async fn terminate_session(db: &TestDb, pid: i32) -> Result<()> {
    let killed: bool = db
        .admin
        .query_one("SELECT pg_terminate_backend($1)", &[&pid])
        .await?
        .try_get(0)?;
    ensure!(killed, "the lease session {pid} was not terminated");
    Ok(())
}

/// Waits until a probe sees no holder of the lease's PostgreSQL lock. The bound is a hang guard.
async fn wait_until_unowned(probe: &ActiveDaemonLease) -> Result<()> {
    timeout(Duration::from_secs(10), async {
        while probe.owner().await?.is_some() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        anyhow::Ok(())
    })
    .await
    .context("the lease lock was never freed")?
}

/// Waits until the lease has seen its session's socket close, with nothing heartbeating it.
async fn wait_until_closed(lease: &ActiveDaemonLease) -> Result<()> {
    timeout(Duration::from_secs(10), async {
        while lease.fence().is_some() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .context("the lease never saw its session close")
}

/// [`wait_until_closed`] for a lease behind its lock, taking the lock for each look.
#[cfg(unix)]
async fn wait_until_closed_under_lock(
    lease: &gobby_daemon::lease::NamedLock<ActiveDaemonLease>,
) -> Result<()> {
    timeout(Duration::from_secs(10), async {
        while lease.lock_as("test").await.fence().is_some() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .context("the lease never saw its session close")
}

impl TestDb {
    async fn epoch(&self, token: &str) -> Result<Option<i64>> {
        Ok(self.runtime_row(token).await?.map(|(epoch, _)| epoch))
    }
}

/// A hub runtime whose only lease is `lease`, with a standby backend.
#[cfg(unix)]
fn lease_runtime(lease: ActiveDaemonLease) -> gobby_daemon::state::RuntimeState {
    use gobby_daemon::lease::NamedLock;
    use gobby_daemon::lifecycle::backend::BackendStatus;
    use tokio::sync::watch;
    gobby_daemon::state::RuntimeState {
        mode: LeaseMode::Hub,
        lease_state: Some(lease.subscribe()),
        lease: Some(Arc::new(NamedLock::new(lease))),
        backend: watch::channel(BackendStatus::Standby).1,
        backend_wait: watch::channel(None).1,
        write_token: Default::default(),
        terminal_route: Default::default(),
    }
}

#[cfg(unix)]
#[tokio::test]
async fn a_lease_published_lost_with_a_live_session_is_not_active_and_is_not_reacquired_by_admin()
-> Result<()> {
    use super::lifecycle::lease_admin;
    use axum::http::{Method, StatusCode};
    database_case(async |db| {
        let token = Uuid::new_v4().to_string();
        let lease = db.lease(&token)?;
        let application = lease.application_name().to_owned();
        let runtime = lease_runtime(lease);
        let held = runtime.lease.as_ref().context("the runtime's lease")?;
        let lifetime = held
            .lock_as("test")
            .await
            .begin_lifetime(None)
            .await?
            .context("the first lifetime did not acquire")?;
        held.lock_as("test").await.loss().publish();
        let session: i32 = db
            .admin
            .query_one(
                "SELECT pid FROM pg_stat_activity WHERE application_name = $1",
                &[&application],
            )
            .await?
            .try_get(0)?;

        let (status, body) = lease_admin(&runtime, Method::GET, "/api/admin/lease/status").await?;
        assert_eq!(status, StatusCode::OK, "{body}");
        assert_eq!(
            body["mode"], "standby",
            "a lease published lost reads as active: {body}"
        );
        assert_eq!(body["held"], true, "{body}");
        assert_eq!(body["owner_pid"], session, "{body}");
        let (status, body) =
            lease_admin(&runtime, Method::POST, "/api/admin/lease/promote").await?;
        assert_eq!(status, StatusCode::CONFLICT, "{body}");
        assert_eq!(body["detail"], LIFETIME_OPEN, "{body}");
        let (status, body) = lease_admin(
            &runtime,
            Method::POST,
            "/api/admin/lease/recover?stale_after_seconds=0.001",
        )
        .await?;
        assert_eq!(status, StatusCode::CONFLICT, "{body}");
        assert_eq!(body["detail"], LIFETIME_OPEN, "{body}");
        assert_eq!(db.epoch(&token).await?, Some(1), "admin rotated the epoch");
        let listed = db
            .admin
            .query("SELECT 1 FROM pg_stat_activity WHERE pid = $1", &[&session])
            .await?;
        assert_eq!(listed.len(), 1, "admin ended the lease session");

        let _lifetime = held
            .lock_as("test")
            .await
            .begin_lifetime(Some(lifetime))
            .await?
            .context("the next lifetime did not acquire")?;
        assert_eq!(db.epoch(&token).await?, Some(2));
        let (status, body) =
            lease_admin(&runtime, Method::POST, "/api/admin/lease/promote").await?;
        assert_eq!(status, StatusCode::OK, "{body}");
        assert_eq!(
            body,
            serde_json::json!({"promoting": false, "mode": "active"})
        );
        held.lock_as("test").await.release().await
    })
    .await
}

#[tokio::test]
async fn an_open_lifetime_refuses_every_other_acquisition_until_the_run_loop_ends_it() -> Result<()>
{
    database_case(async |db| {
        let token = Uuid::new_v4().to_string();
        let mut lease = db.lease(&token)?;
        let probe = db.lease(&token)?;
        let mut peer = db.lease(&token)?;
        let state = lease.subscribe();
        let first = lease
            .begin_lifetime(None)
            .await?
            .context("the first lifetime did not acquire")?;
        let pid = probe.owner().await?.context("the lease has no owner")?.pid;
        terminate_session(db, pid).await?;
        ensure!(
            lease.heartbeat().await.is_err(),
            "a heartbeat on the terminated session succeeded"
        );
        wait_until_unowned(&probe).await?;

        assert!(
            lease.try_acquire().await.is_err(),
            "an open lifetime let try_acquire acquire"
        );
        assert_eq!(db.epoch(&token).await?, Some(1));
        assert_eq!(*state.borrow(), LeaseState::Lost);
        let error = lease
            .recover_stale_owner(Duration::from_millis(1))
            .await
            .expect_err("an open lifetime let recovery run");
        assert!(error.to_string().contains(LIFETIME_OPEN), "{error:#}");
        let second = lease
            .begin_lifetime(Some(first))
            .await?
            .expect("the run loop's begin acquires the next lifetime");
        assert_eq!(db.epoch(&token).await?, Some(2));
        assert_eq!(*state.borrow(), LeaseState::Active);
        {
            let _third = lease
                .begin_lifetime(Some(second))
                .await?
                .expect("a begin on a healthy lease keeps it");
            assert_eq!(
                db.epoch(&token).await?,
                Some(2),
                "a healthy handoff rotated the epoch"
            );
        }
        // The third lifetime was abandoned, not ended.
        assert_eq!(*state.borrow(), LeaseState::Lost);
        assert!(lease.fence().is_none());
        assert!(
            lease.try_acquire().await.is_err(),
            "an abandoned lifetime reopened acquisition"
        );
        assert!(
            lease.begin_lifetime(None).await.is_err(),
            "an abandoned lifetime let a new one begin"
        );
        assert_eq!(db.epoch(&token).await?, Some(2));
        assert!(
            !peer.try_acquire().await?,
            "an abandoned lifetime gave up the lock"
        );
        Ok(())
    })
    .await
}

#[tokio::test]
async fn a_lease_published_lost_fails_its_heartbeat_and_keeps_its_session() -> Result<()> {
    database_case(async |db| {
        let token = Uuid::new_v4().to_string();
        let mut lease = db.lease(&token)?;
        let probe = db.lease(&token)?;
        ensure!(lease.try_acquire().await?, "fixture must hold the lease");
        let state = lease.subscribe();
        let pid = probe.owner().await?.context("the lease has no owner")?.pid;
        lease.loss().publish();

        assert!(
            lease.heartbeat().await.is_err(),
            "a lease published lost passed its heartbeat"
        );
        assert!(lease.fence().is_none());
        assert_eq!(
            probe.owner().await?.map(|owner| owner.pid),
            Some(pid),
            "the failed heartbeat closed the lease session"
        );
        assert!(lease.release().await.is_ok());
        assert!(probe.owner().await?.is_none(), "the release kept the lock");
        assert_eq!(*state.borrow(), LeaseState::Standby);
        lease.loss().publish();
        assert_eq!(
            *state.borrow(),
            LeaseState::Standby,
            "a loss published after the release replaced Standby"
        );
        Ok(())
    })
    .await
}

#[cfg(unix)]
#[tokio::test]
async fn a_promote_on_a_dead_session_the_supervisor_has_not_detected_is_refused_without_a_heartbeat()
-> Result<()> {
    use super::lifecycle::lease_admin;
    use axum::http::{Method, StatusCode};
    database_case(async |db| {
        let token = Uuid::new_v4().to_string();
        let lease = db.lease(&token)?;
        let probe = db.lease(&token)?;
        let state = lease.subscribe();
        let runtime = lease_runtime(lease);
        let held = runtime.lease.as_ref().context("the runtime's lease")?;
        let _lifetime = held
            .lock_as("test")
            .await
            .begin_lifetime(None)
            .await?
            .context("the lifetime did not acquire")?;
        let pid = probe.owner().await?.context("the lease has no owner")?.pid;
        terminate_session(db, pid).await?;
        wait_until_closed_under_lock(held).await?;
        ensure!(
            *state.borrow() == LeaseState::Active,
            "something heartbeated the dead session"
        );

        for path in [
            "/api/admin/lease/promote",
            "/api/admin/lease/promote",
            "/api/admin/lease/recover?stale_after_seconds=0.001",
        ] {
            let (status, body) = lease_admin(&runtime, Method::POST, path).await?;
            assert_eq!(status, StatusCode::CONFLICT, "{path}: {body}");
            assert_eq!(body["detail"], LIFETIME_OPEN, "{path}: {body}");
            assert_eq!(
                *state.borrow(),
                LeaseState::Active,
                "{path} heartbeated the dead session"
            );
            assert_eq!(db.epoch(&token).await?, Some(1), "{path} rotated the epoch");
        }
        Ok(())
    })
    .await
}

#[tokio::test]
async fn a_lifetime_whose_session_died_is_ended_by_the_failed_begin_and_the_next_begin_acquires()
-> Result<()> {
    database_case(async |db| {
        let token = Uuid::new_v4().to_string();
        let mut lease = db.lease(&token)?;
        let probe = db.lease(&token)?;
        let state = lease.subscribe();
        let first = lease
            .begin_lifetime(None)
            .await?
            .context("the first lifetime did not acquire")?;
        let pid = probe.owner().await?.context("the lease has no owner")?.pid;
        terminate_session(db, pid).await?;
        wait_until_closed(&lease).await?;
        wait_until_unowned(&probe).await?;

        assert!(
            lease.begin_lifetime(Some(first)).await.is_err(),
            "a begin on a dead session succeeded"
        );
        assert_eq!(*state.borrow(), LeaseState::Lost);
        assert!(lease.fence().is_none());
        assert_eq!(db.epoch(&token).await?, Some(1));
        let next = lease.begin_lifetime(None).await?;
        assert!(next.is_some(), "the failed begin left acquisition refused");
        assert_eq!(db.epoch(&token).await?, Some(2));
        assert_eq!(*state.borrow(), LeaseState::Active);
        lease.release().await
    })
    .await
}

#[test]
fn node_mode_is_refused_before_connecting() {
    // Pool::build does not connect, so an unreachable URL proves no connection.
    let unreachable = pool("postgresql://test@127.0.0.1:1/test").expect("lazy pool");
    let node = ActiveDaemonLease::new(unreachable, "machine", "token", LeaseMode::Node);
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
        let application_name = owner.application_name().to_owned();
        let mut acquire = Box::pin(owner.try_acquire());
        // Hang guards sized for host-load stalls. The asserted bound is the 5 s operation timeout.
        let observed = timeout(Duration::from_secs(30), async {
            loop {
                db.admin.simple_query("SELECT pg_stat_clear_snapshot()").await?;
                let blocked: bool = db.admin.query_one(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity \
                     WHERE application_name = $1 AND wait_event_type = 'Lock' \
                     AND query LIKE 'INSERT INTO deployment_runtime%')",
                    &[&application_name],
                ).await?.try_get(0)?;
                if blocked {
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
        timeout(Duration::from_secs(30), async {
            while successor.owner().await?.is_some() {
                tokio::task::yield_now().await;
            }
            anyhow::Ok(())
        }).await??;
        // A second blocked acquisition reaches the five-second operation bound.
        let result = timeout(Duration::from_secs(30), owner.try_acquire()).await?;
        assert!(result.is_err());
        timeout(Duration::from_secs(30), async {
            while successor.owner().await?.is_some() {
                tokio::task::yield_now().await;
            }
            anyhow::Ok(())
        }).await??;
        db.admin.batch_execute("ROLLBACK").await?;
        assert!(db.runtime_row("blocked").await?.is_none());
        assert!(successor.try_acquire().await?);
        assert_eq!(successor.fence().expect("successor fence").epoch, 1);
        successor.release().await
    }).await
}

#[tokio::test]
async fn acquisition_checkout_and_connect_count_against_the_operation_bound() -> Result<()> {
    database_case(async |db| {
        let mut owner = db.lease("checkout")?;
        // Every pooled connection is checked out, so the owner's checkout waits. The
        // connections return after four seconds, and the insert then blocks on the lock.
        let mut held = Vec::new();
        for _ in 0..8 {
            held.push(db.pool.get().await?);
        }
        db.admin
            .batch_execute(&format!(
                "BEGIN; LOCK TABLE {}.deployment_runtime IN ACCESS EXCLUSIVE MODE",
                db.schema
            ))
            .await?;
        let returned = tokio::spawn(async move {
            // test-quality: allow SLEEP_IN_TEST -- the checkout must wait for a bounded window shorter than the 5 s operation bound before the connections return; the pool has no event to await, and paused time would advance over the real database I/O
            tokio::time::sleep(Duration::from_secs(4)).await;
            drop(held);
        });
        // The operation bound covers the whole attempt, checkout included.
        let result = timeout(Duration::from_secs(7), owner.try_acquire()).await?;
        assert!(result.is_err());
        returned.await?;
        db.admin.batch_execute("ROLLBACK").await?;
        Ok(())
    })
    .await
}

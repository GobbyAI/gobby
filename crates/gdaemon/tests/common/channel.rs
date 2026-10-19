//! Shared live-channel fixtures; always use the protected isolated test hub.
use super::TIMEOUT;
use anyhow::{Context, Result, ensure};
use futures_util::{FutureExt, SinkExt, StreamExt};
use gobby_core::{
    bootstrap::parse_hub_database_bootstrap,
    postgres_pool::{Pool, PoolSettings},
};
use gobby_daemon::{
    front_door::{
        FrontDoor, FrontDoorState,
        auth::{AuthState, PostgresKeyResolver},
        health::BackendState,
        routes::{FAMILIES, RouteTable},
    },
    lifecycle::pid_file::PidFileClaim,
    nodes::{HubNodes, NativeRoutes, channel::ChannelTiming, registry::Registry},
    serve::serve_doors,
    state::NodeServices,
};
use std::{
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::Arc,
};
use tokio::{
    net::{TcpListener, TcpStream},
    task::JoinHandle,
    time::{Duration, sleep, timeout},
};
use tokio_postgres::{Client as PgClient, NoTls};
use tokio_tungstenite::{
    WebSocketStream,
    tungstenite::{Message as PeerMessage, client::IntoClientRequest},
};
use uuid::Uuid;
pub const KEY: &str = "gobby_003aUlTJC7tjlCTQj2uNU3MFagCXG9LRKRcwGkBIDlf1Yo7hP";
pub struct Node {
    pub addr: SocketAddr,
    task: JoinHandle<()>,
    _claim: PidFileClaim,
    _home: tempfile::TempDir,
}

impl Drop for Node {
    fn drop(&mut self) {
        self.task.abort();
    }
}

pub fn write_bootstrap(
    home: &Path,
    origin: &str,
    pin: Option<&Path>,
    ports: Option<[u16; 2]>,
) -> PathBuf {
    let pin = pin.map_or(String::new(), |path| {
        format!("hub_cert: {}\n", path.display())
    });
    let ports = ports.map_or(String::new(), |ports| {
        format!("daemon_port: {}\nwebsocket_port: {}\n", ports[0], ports[1])
    });
    let path = home.join("bootstrap.yaml");
    std::fs::write(
        &path,
        format!(
            "datastore_mode: remote\nhub_daemon_url: {origin}\napi_key: {KEY}\n\
             bind_host: 127.0.0.1\n{pin}{ports}"
        ),
    )
    .expect("node bootstrap");
    path
}

pub async fn node(origin: &str, pin: Option<&Path>, ws_listener: bool) -> Node {
    let home = tempfile::tempdir().expect("node home");
    let path = write_bootstrap(home.path(), origin, pin, None);
    let bootstrap =
        parse_hub_database_bootstrap(&std::fs::read_to_string(&path).expect("read node"))
            .expect("parse node")
            .expect("node config");
    let (services, claim) = NodeServices::prepare(&bootstrap, &path, home.path())
        .expect("prepare node")
        .expect("node claim");
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("node bind");
    let addr = listener.local_addr().expect("node address");
    let task = tokio::spawn(async move {
        serve_doors(
            vec![(listener, FrontDoor::node(services, ws_listener))],
            None,
            gobby_daemon::heartbeat::HeartbeatHost::new(),
            std::future::pending(),
        )
        .await
        .expect("node serve");
    });
    Node {
        addr,
        task,
        _claim: claim,
        _home: home,
    }
}

// Channel acceptance tests use real monotonic time and their own schema. Database
// waits must not auto-advance Tokio's timers while PostgreSQL is still running.
pub struct ChannelDb {
    pub admin: PgClient,
    pub url: String,
    pub schema: String,
    pub machine: Uuid,
    pub key_id: Uuid,
}

pub fn channel_timing() -> ChannelTiming {
    ChannelTiming {
        ping_interval: Duration::from_millis(40),
        backoff_start: Duration::from_millis(50),
        backoff_max: Duration::from_millis(200),
        attempt_timeout: Duration::from_millis(400),
        hello_timeout: Duration::from_secs(3),
        idle_timeout: Duration::from_secs(2),
        key_recheck: Duration::from_millis(600),
        recheck_deadline: Duration::from_millis(200),
        heartbeat_write: Duration::from_millis(150),
    }
}

pub async fn channel_database(case: impl AsyncFnOnce(&ChannelDb) -> Result<()>) -> Result<()> {
    let url = std::env::var("DATABASE_URL")?;
    let config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("gobby_test")
            && config.get_hosts() == [tokio_postgres::config::Host::Tcp("127.0.0.1".into())]
            && config.get_ports() == [60892]
            && std::env::var("GOBBY_TEST_PROTECT").as_deref() == Ok("1"),
        "node channel tests require the protected isolated test hub"
    );
    let (admin, driver) = config.connect(NoTls).await?;
    let driver = tokio::spawn(driver);
    timeout(
        Duration::from_secs(30),
        admin.simple_query("SELECT pg_advisory_lock(hashtext('gdaemon-node-channel-tests'), 1)"),
    )
    .await
    .context("waiting for node-channel fixture lock")??;
    let schema = format!("test_node_channel_{}", Uuid::new_v4().simple());
    let machine = Uuid::new_v4();
    let key_id = Uuid::new_v4();
    let hash = gobby_core::api_key_format::hash(KEY);
    admin
        .batch_execute(&format!(
            "CREATE SCHEMA {schema}; GRANT USAGE ON SCHEMA {schema} TO gobby_daemon_runtime; \
         CREATE TABLE {schema}.machines(id uuid PRIMARY KEY, ref integer, \
         hostname text, os text, label text, tailscale_name text, owner_user_id uuid, \
         first_seen timestamptz, last_seen timestamptz, \
         last_heartbeat_at timestamptz, node_version text); \
         CREATE TABLE {schema}.api_keys(id uuid PRIMARY KEY, \
         user_id uuid NOT NULL, machine_id uuid NOT NULL, \
         key_hash text NOT NULL, revoked_at timestamptz, last_used_at timestamptz); \
         GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {schema} \
         TO gobby_daemon_runtime; \
         INSERT INTO {schema}.machines(id) VALUES ('{machine}'); \
         INSERT INTO {schema}.api_keys(id,user_id,machine_id,key_hash) \
         VALUES ('{key_id}','{}','{machine}','{hash}')",
            Uuid::new_v4()
        ))
        .await?;
    let separator = if url.contains('?') { '&' } else { '?' };
    let db = ChannelDb {
        admin,
        schema: schema.clone(),
        url: format!("{url}{separator}options=-csearch_path%3D{schema}"),
        machine,
        key_id,
    };
    let outcome = std::panic::AssertUnwindSafe(case(&db)).catch_unwind().await;
    db.admin.batch_execute("ROLLBACK").await?;
    let cleanup = db
        .admin
        .batch_execute(&format!("DROP SCHEMA {schema} CASCADE"))
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
                eprintln!("channel cleanup: {error}");
            }
            std::panic::resume_unwind(panic)
        }
    }
}

impl ChannelDb {
    pub fn pool(&self) -> Result<Arc<Pool>> {
        Ok(Arc::new(Pool::build(
            &self.url,
            PoolSettings {
                max_size: 4,
                application_name: "gobby-gdaemon-nodes-test".into(),
                acquire_timeout: Duration::from_secs(2),
            },
        )?))
    }

    pub async fn row(&self) -> Result<(Option<f64>, Option<String>)> {
        let row = self
            .admin
            .query_one(
                &format!(
                    "SELECT extract(epoch FROM last_heartbeat_at)::double precision,node_version \
                 FROM {}.machines WHERE id=$1::text::uuid",
                    self.schema
                ),
                &[&self.machine.to_string()],
            )
            .await?;
        Ok((row.get(0), row.get(1)))
    }

    pub async fn revoke(&self) -> Result<()> {
        // Commit independently of the transaction holding a machine-row lock.
        let (admin, driver) = tokio_postgres::connect(&self.url, NoTls).await?;
        let driver = tokio::spawn(driver);
        let result = admin
            .execute(
                &format!(
                    "UPDATE {}.api_keys SET revoked_at=now() WHERE id=$1::text::uuid",
                    self.schema
                ),
                &[&self.key_id.to_string()],
            )
            .await;
        drop(admin);
        driver.abort();
        let _ = driver.await;
        result?;
        Ok(())
    }

    pub async fn lock_machine(&self) -> Result<()> {
        self.admin
            .batch_execute(&format!(
                "BEGIN; SELECT id FROM {}.machines FOR UPDATE",
                self.schema
            ))
            .await?;
        Ok(())
    }

    pub async fn lock_keys(&self) -> Result<()> {
        self.admin
            .batch_execute(&format!(
                "BEGIN; LOCK TABLE {}.api_keys IN ACCESS EXCLUSIVE MODE",
                self.schema
            ))
            .await?;
        Ok(())
    }

    pub async fn unlock(&self) -> Result<()> {
        self.admin.batch_execute("COMMIT").await?;
        Ok(())
    }

    pub async fn wait_blocked(&self, relation: &str) -> Result<()> {
        timeout(TIMEOUT, async {
            let mut poll = tokio::time::interval(Duration::from_millis(5));
            loop {
                // The locking transaction otherwise retains its first activity snapshot.
                self.admin
                    .simple_query("SELECT pg_stat_clear_snapshot()")
                    .await?;
                let row = self
                    .admin
                    .query_one(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity \
                     WHERE application_name='gobby-gdaemon-nodes-test' \
                     AND wait_event_type='Lock' AND query LIKE $1)",
                        &[&format!("%{relation}%")],
                    )
                    .await?;
                if row.get::<_, bool>(0) {
                    return Ok::<_, anyhow::Error>(());
                }
                poll.tick().await;
            }
        })
        .await
        .with_context(|| format!("waiting for blocked {relation} query"))??;
        Ok(())
    }
}

pub struct ChannelHub {
    pub door: FrontDoor,
    pub addr: SocketAddr,
    pub nodes: Arc<HubNodes>,
    task: JoinHandle<()>,
    pub _home: tempfile::TempDir,
}
impl Drop for ChannelHub {
    fn drop(&mut self) {
        self.task.abort();
    }
}

pub async fn channel_hub(db: &ChannelDb, timing: ChannelTiming) -> Result<ChannelHub> {
    channel_door(db, timing, None).await
}

pub async fn channel_door(
    db: &ChannelDb,
    timing: ChannelTiming,
    native: Option<NativeRoutes>,
) -> Result<ChannelHub> {
    let pool = db.pool()?;
    let nodes = Arc::new(HubNodes {
        db: pool.clone(),
        registry: Registry::default(),
        timing,
    });
    warm_channel_pool(&nodes.db).await?;
    let home = tempfile::tempdir()?;
    let path = write_bootstrap(home.path(), "http://127.0.0.1:1", None, None);
    let auth = Arc::new(AuthState::new(
        "test-secret".into(),
        Some(Arc::new(PostgresKeyResolver::new(pool))),
        path,
    )?);
    let state = FrontDoorState {
        native: native.unwrap_or_else(|| NativeRoutes::Hub(nodes.clone())),
        ..FrontDoorState::new("127.0.0.1:1".parse()?, BackendState::Down, auth)
    };
    let table = RouteTable::new(FAMILIES, &Default::default(), &state);
    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr()?;
    let door = FrontDoor::new(state, table);
    let serving = door.clone();
    let task = tokio::spawn(async move {
        serve_doors(
            vec![(listener, serving)],
            None,
            gobby_daemon::heartbeat::HeartbeatHost::new(),
            std::future::pending(),
        )
        .await
        .expect("channel hub");
    });
    Ok(ChannelHub {
        door,
        addr,
        nodes,
        task,
        _home: home,
    })
}

pub async fn warm_channel_pool(pool: &Pool) -> Result<()> {
    // Create connections sequentially before starting the channel's timed proof
    // so cold creation does not contend. Hold both so the second checkout cannot
    // reuse the first; a held heartbeat needs an independent key-check connection.
    let first = pool
        .get()
        .await
        .context("warming first channel connection")?;
    let second = pool
        .get()
        .await
        .context("warming second channel connection")?;
    drop((first, second));
    assert!(pool.status().available >= 2);
    Ok(())
}

pub type ChannelPeer = WebSocketStream<TcpStream>;
pub async fn open_channel(addr: SocketAddr) -> Result<ChannelPeer> {
    let mut request = format!("ws://{addr}/api/nodes/channel").into_client_request()?;
    request
        .headers_mut()
        .insert("authorization", format!("Bearer {KEY}").parse()?);
    let stream = TcpStream::connect(addr).await?;
    let result = timeout(TIMEOUT, tokio_tungstenite::client_async(request, stream)).await?;
    assert!(
        result.is_ok(),
        "key-authenticated channel must upgrade: {result:?}"
    );
    Ok(result?.0)
}
pub async fn hello(peer: &mut ChannelPeer, version: &str) -> Result<()> {
    peer.send(PeerMessage::Text(
        serde_json::json!({"type":"hello","node_version":version,"platform":"test/os"})
            .to_string()
            .into(),
    ))
    .await?;
    Ok(())
}
pub async fn ack(peer: &mut ChannelPeer, machine: Uuid) -> Result<()> {
    let frame = timeout(TIMEOUT, peer.next())
        .await
        .context("waiting for channel ack")?
        .expect("ack frame")?;
    let value: serde_json::Value = serde_json::from_str(frame.to_text()?)?;
    assert_eq!(
        value,
        serde_json::json!({"type":"ack","machine_id":machine.to_string()})
    );
    Ok(())
}
pub async fn close_code(peer: &mut ChannelPeer, code: u16) -> Result<()> {
    timeout(TIMEOUT, async {
        loop {
            match peer.next().await {
                Some(Ok(PeerMessage::Close(Some(frame)))) => {
                    assert_eq!(u16::from(frame.code), code);
                    return Ok(());
                }
                Some(Ok(PeerMessage::Pong(_))) => {}
                other => anyhow::bail!("expected close {code}, got {other:?}"),
            }
        }
    })
    .await
    .with_context(|| format!("waiting for channel close {code}"))?
}
pub async fn wait_registered(nodes: &HubNodes, machine: Uuid) -> Result<Uuid> {
    timeout(TIMEOUT, async {
        loop {
            if let Some(id) = nodes.registry.connection_id(machine) {
                return id;
            }
            sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .map_err(Into::into)
}

//! Lifecycle boundaries use real Unix control sockets and an identity-checked process.
use std::future::Future;
use std::pin::Pin;
use std::process::{Child, Command};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use gobby_core::bootstrap::RouteBackend;
use gobby_terminals::control::ControlClient;
use gobby_terminals::family::{RouteLatch, SupervisorMode, TerminalFamily, WsRoutes};
use gobby_terminals::host::PostgresEpochAuthority;
use gobby_terminals::host::READY_DEADLINE;
use gobby_terminals::host::{
    EpochAuthority, EpochBinding, HostFailure, HostOptions, HostStatus, HostSupervisor,
};
use serde_json::{Value, json};
use tempfile::TempDir;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixListener;
use tokio::task::JoinHandle;

struct Rows(Vec<EpochBinding>);

impl EpochAuthority for Rows {
    fn bindings<'a>(
        &'a self,
        epoch: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Vec<EpochBinding>, HostFailure>> + Send + 'a>> {
        Box::pin(async move {
            Ok(self
                .0
                .iter()
                .filter(|row| row.host_epoch == epoch)
                .cloned()
                .collect())
        })
    }
}

struct HostFixture {
    dir: TempDir,
    process: Child,
    connections: Arc<AtomicUsize>,
    task: JoinHandle<()>,
}

impl HostFixture {
    async fn new(hello_error: Option<&str>, protocol: u32, bad_pid: bool) -> Self {
        let dir = TempDir::new().unwrap();
        let binary = dir.path().join("gterm");
        std::fs::copy("/bin/sleep", &binary).unwrap();
        #[cfg(target_os = "macos")]
        assert!(
            Command::new("/usr/bin/codesign")
                .args(["--force", "--sign", "-"])
                .arg(&binary)
                .output()
                .unwrap()
                .status
                .success(),
            "identity fixture executable must be signed before launch"
        );
        let process = Command::new(&binary).arg("600").spawn().unwrap();
        let pid = process.id();
        std::fs::write(dir.path().join("gterm.pid"), pid.to_string()).unwrap();
        std::fs::write(dir.path().join("gterm-control.token"), "fixture-token\n").unwrap();
        let listener = UnixListener::bind(dir.path().join("gterm-control.sock")).unwrap();
        let connections = Arc::new(AtomicUsize::new(0));
        let count = connections.clone();
        let hello_error = hello_error.map(str::to_owned);
        let task = tokio::spawn(async move {
            loop {
                let (stream, _) = listener.accept().await.unwrap();
                count.fetch_add(1, Ordering::SeqCst);
                let error = hello_error.clone();
                tokio::spawn(async move {
                    let (reader, mut writer) = stream.into_split();
                    let mut reader = BufReader::new(reader);
                    let mut line = String::new();
                    let mut pings = 0;
                    loop {
                        line.clear();
                        if reader.read_line(&mut line).await.unwrap() == 0 {
                            return;
                        }
                        let request: Value = serde_json::from_str(&line).unwrap();
                        if error.as_deref() == Some("pre-io") {
                            return;
                        }
                        if error.as_deref() == Some("stall") {
                            std::future::pending::<()>().await;
                        }
                        if request["method"] == "ping" {
                            pings += 1;
                            if error.as_deref() == Some("post-io") && pings > 1 {
                                return;
                            }
                        }
                        let mut reply = match request["method"].as_str().unwrap() {
                            "hello" => match &error {
                                Some(error) if error != "post-io" => {
                                    json!({"ok": false, "error": error})
                                }
                                _ => {
                                    assert_eq!(request["control_token"], "fixture-token");
                                    json!({"ok": true, "protocol_version": protocol,
                                        "host_epoch": "epoch-E", "version": "fixture", "capabilities": []})
                                }
                            },
                            "ping" => json!({"ok": true, "host_epoch": "epoch-E",
                                "version": "fixture", "host_pid": if bad_pid { pid + 1 } else { pid }}),
                            method => panic!("lifecycle must not mutate host: {method}"),
                        };
                        reply["id"] = request["id"].clone();
                        let mut bytes = serde_json::to_vec(&reply).unwrap();
                        bytes.push(b'\n');
                        if writer.write_all(&bytes).await.is_err() {
                            return;
                        }
                    }
                });
            }
        });
        Self {
            dir,
            process,
            connections,
            task,
        }
    }

    fn options(&self) -> HostOptions {
        HostOptions {
            socket_dir: self.dir.path().to_owned(),
            binary_path: self.dir.path().join("gterm"),
            machine_id: "10000000-0000-4000-8000-000000000001".into(),
            health_interval: Duration::from_secs(60),
            ..HostOptions::default()
        }
    }

    fn alive(&mut self) -> bool {
        let status = self.process.try_wait().unwrap();
        if let Some(status) = status {
            eprintln!(
                "identity fixture pid={} exited: {status:?}",
                self.process.id()
            );
        }
        status.is_none()
    }
}

impl Drop for HostFixture {
    fn drop(&mut self) {
        self.task.abort();
        let _ = self.process.kill();
        let _ = self.process.wait();
    }
}

fn binding(machine: &str, epoch: &str) -> EpochBinding {
    EpochBinding {
        machine_id: machine.into(),
        project_id: "project-A".into(),
        host_epoch: epoch.into(),
        state: "live".into(),
    }
}

#[tokio::test]
async fn same_epoch_adopts_singleflight() {
    let mut host = HostFixture::new(None, 1, false).await;
    let token = std::fs::read(host.dir.path().join("gterm-control.token")).unwrap();
    let socket = std::fs::metadata(host.dir.path().join("gterm-control.sock")).unwrap();
    let supervisor = HostSupervisor::new(
        host.options(),
        Arc::new(Rows(vec![binding(
            "10000000-0000-4000-8000-000000000001",
            "epoch-E",
        )])),
    );
    let (first, second) = tokio::join!(supervisor.start(), supervisor.start());
    let first = first.unwrap();
    assert_eq!(second.unwrap(), first);
    assert_eq!(first.pid, host.process.id());
    assert_eq!(first.epoch, "epoch-E");
    assert!(first.adopted);
    assert_eq!(host.connections.load(Ordering::SeqCst), 1);
    assert_eq!(
        std::fs::read(host.dir.path().join("gterm-control.token")).unwrap(),
        token
    );
    use std::os::unix::fs::MetadataExt;
    assert_eq!(
        std::fs::metadata(host.dir.path().join("gterm-control.sock"))
            .unwrap()
            .ino(),
        socket.ino()
    );
    supervisor.stop().await;
    assert!(host.alive());
}

#[tokio::test]
async fn semantic_epoch_refusal_is_non_destructive() {
    let mut host = HostFixture::new(None, 1, false).await;
    let supervisor = HostSupervisor::new(
        host.options(),
        Arc::new(Rows(vec![binding("M2", "epoch-E")])),
    );
    assert_eq!(supervisor.start().await, Err(HostFailure::EpochRefused));
    assert!(supervisor.retry_armed());
    assert_eq!(host.connections.load(Ordering::SeqCst), 1);
    supervisor.stop().await;
    assert!(host.alive());
}

#[tokio::test]
async fn route_mode_has_exactly_one_supervisor() {
    let mut host = HostFixture::new(None, 1, false).await;
    for route in [RouteBackend::Proxy, RouteBackend::Compare] {
        let latch = RouteLatch::new(route, WsRoutes::Registered).unwrap();
        let family = TerminalFamily::new(&latch, host.options(), Arc::new(Rows(vec![])));
        assert_eq!(family.mode(), SupervisorMode::Observer);
        assert!(family.start().await.unwrap().is_none());
        family.stop().await;
    }
    assert_eq!(host.connections.load(Ordering::SeqCst), 0);
    let latch = RouteLatch::new(RouteBackend::Native, WsRoutes::Registered).unwrap();
    let family = TerminalFamily::new(&latch, host.options(), Arc::new(Rows(vec![])));
    assert_eq!(family.mode(), SupervisorMode::Owner);
    assert_eq!(
        family.start().await.unwrap().unwrap().pid,
        host.process.id()
    );
    family.stop().await;
    assert!(host.alive());
}

#[tokio::test]
async fn adoption_result_subtypes_are_exhaustive() {
    let cases = [
        (Some("invalid_token"), 1, false, HostFailure::TokenMismatch),
        (
            Some("unauthenticated"),
            1,
            false,
            HostFailure::Authentication,
        ),
        (
            Some("unsupported_protocol"),
            1,
            false,
            HostFailure::ProtocolMismatch,
        ),
        (None, 2, false, HostFailure::ProtocolMismatch),
        (None, 1, true, HostFailure::PidMismatch),
    ];
    for (error, protocol, bad_pid, expected) in cases {
        let mut host = HostFixture::new(error, protocol, bad_pid).await;
        let token = std::fs::read(host.dir.path().join("gterm-control.token")).unwrap();
        // Foreign bindings must never reclassify failures before coherent adoption.
        let supervisor = HostSupervisor::new(
            host.options(),
            Arc::new(Rows(vec![binding("M2", "epoch-E")])),
        );
        assert_eq!(supervisor.start().await, Err(expected));
        assert!(supervisor.retry_armed());
        assert_eq!(host.connections.load(Ordering::SeqCst), 1);
        assert_eq!(
            std::fs::read(host.dir.path().join("gterm-control.token")).unwrap(),
            token
        );
        supervisor.stop().await;
        assert!(
            host.alive(),
            "fixture exited: error={error:?}, protocol={protocol}, bad_pid={bad_pid}, status={:?}",
            host.process.try_wait().unwrap()
        );
    }
    let mut host = HostFixture::new(Some("pre-io"), 1, false).await;
    let supervisor = HostSupervisor::new(host.options(), Arc::new(Rows(vec![])));
    assert!(matches!(
        supervisor.start().await,
        Err(HostFailure::PreAdoptionIo(_))
    ));
    supervisor.stop().await;
    assert!(host.alive());

    let mut host = HostFixture::new(Some("post-io"), 1, false).await;
    let mut options = host.options();
    options.health_interval = Duration::from_millis(20);
    let supervisor = HostSupervisor::new(options, Arc::new(Rows(vec![])));
    let mut status = supervisor.subscribe();
    supervisor.start().await.unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if matches!(
                &*status.borrow_and_update(),
                HostStatus::Unavailable(HostFailure::PostAdoptionIo(_))
            ) {
                break;
            }
            status.changed().await.unwrap();
        }
    })
    .await
    .unwrap();
    assert!(supervisor.retry_armed());
    supervisor.stop().await;
    assert!(host.alive());

    let absent = TempDir::new().unwrap();
    let options = HostOptions {
        socket_dir: absent.path().to_owned(),
        binary_path: absent.path().join("missing-gterm"),
        machine_id: "10000000-0000-4000-8000-000000000001".into(),
        ..HostOptions::default()
    };
    let supervisor = HostSupervisor::new(options, Arc::new(Rows(vec![])));
    assert_eq!(supervisor.start().await, Err(HostFailure::GtermMissing));
    supervisor.stop().await;
    assert!(!absent.path().join("gterm-control.token").exists());

    let mut host = HostFixture::new(Some("stall"), 1, false).await;
    let supervisor = HostSupervisor::new(host.options(), Arc::new(Rows(vec![])));
    let before = tokio::time::Instant::now();
    assert_eq!(supervisor.start().await, Err(HostFailure::HostStartTimeout));
    assert!(before.elapsed() <= Duration::from_millis(2800));
    assert!(supervisor.retry_armed());
    supervisor.stop().await;
    assert!(host.alive());
}

#[tokio::test]
async fn explicit_drain_uses_existing_host_shutdown_wire() -> anyhow::Result<()> {
    let (client_stream, host_stream) = tokio::io::duplex(4096);
    let server = tokio::spawn(async move {
        let (read, mut write) = tokio::io::split(host_stream);
        let mut read = BufReader::new(read);
        let mut line = String::new();
        read.read_line(&mut line).await?;
        let hello: Value = serde_json::from_str(&line)?;
        assert_eq!(hello["method"], "hello");
        write
            .write_all(
                b"{\"id\":\"1\",\"ok\":true,\"protocol_version\":1,\"host_epoch\":\"epoch-E\",\"version\":\"fixture\"}\n",
            )
            .await?;
        for (id, grace_ms) in [(2, 0), (3, 1250)] {
            line.clear();
            read.read_line(&mut line).await?;
            let request: Value = serde_json::from_str(&line)?;
            assert_eq!(
                request,
                json!({"id": id.to_string(), "method": "host_shutdown",
                    "grace_ms": grace_ms, "operation_seq": id - 1})
            );
            let reply = json!({"id": id.to_string(), "ok": true, "draining": true});
            write.write_all(format!("{reply}\n").as_bytes()).await?;
        }
        anyhow::Ok(())
    });
    let mut client = ControlClient::new(client_stream);
    client.hello("fixture-token").await?;
    for grace_ms in [0, 1250] {
        let reply = tokio::time::timeout(READY_DEADLINE, client.host_shutdown(grace_ms)).await??;
        assert_eq!(reply["draining"], true);
    }
    server.await??;
    Ok(())
}

struct OwnedHubSchema {
    admin_url: String,
    schema: String,
}

impl OwnedHubSchema {
    async fn new() -> anyhow::Result<Self> {
        let admin_url = std::env::var("GOBBY_TERMINALS_TEST_DATABASE_URL")?;
        anyhow::ensure!(
            admin_url.starts_with("postgresql://gobby_test:gobby_test@127.0.0.1:60892/")
                && admin_url.ends_with("/gobby_test"),
            "terminal lifecycle tests require the explicitly selected isolated test hub"
        );
        let fixture = Self {
            admin_url,
            schema: format!("gobby_terminals_{}", uuid::Uuid::new_v4().simple()),
        };
        let url = fixture.admin_url.clone();
        let schema = fixture.schema.clone();
        tokio::task::spawn_blocking(move || {
            let mut admin = gobby_core::postgres::connect_readwrite(&url)?;
            admin.batch_execute(&format!(
                "SET statement_timeout = 5000;
                 CREATE SCHEMA {schema};
                 CREATE TABLE {schema}.terminals (
                     machine_id text NOT NULL, project_id uuid NOT NULL,
                     host_epoch text, state text NOT NULL, backend text NOT NULL);
                 CREATE TABLE {schema}.config_store (key text PRIMARY KEY, value text NOT NULL);
                 GRANT USAGE ON SCHEMA {schema} TO gobby_daemon_runtime;
                 GRANT SELECT ON {schema}.terminals TO gobby_daemon_runtime;
                 GRANT SELECT ON {schema}.config_store TO gobby_daemon_runtime;
                 INSERT INTO {schema}.terminals VALUES
                 ('10000000-0000-4000-8000-000000000001', '00000000-0000-0000-0000-000000000001', 'epoch-E', 'live', 'native'),
                 ('10000000-0000-4000-8000-000000000001', '00000000-0000-0000-0000-000000000002', 'epoch-E', 'orphaned', 'native'),
                     ('M2', '00000000-0000-0000-0000-000000000001', 'other-epoch', 'live', 'native'),
                     ('M2', '00000000-0000-0000-0000-000000000001', NULL, 'live', 'tmux');"
            ))?;
            let epochs = include_str!("../../gcore/assets/schema/migrations/468_terminal_host_epochs.sql");
            admin.batch_execute(&epochs.replace("public.", &format!("{schema}.")))?;
            anyhow::Ok(())
        })
        .await??;
        Ok(fixture)
    }

    fn pool(&self) -> anyhow::Result<gobby_core::postgres_pool::Pool> {
        Ok(gobby_core::postgres_pool::Pool::build(
            &self.database_url(),
            gobby_core::postgres_pool::PoolSettings {
                max_size: 2,
                application_name: "gobby-gdaemon-terminal-lifecycle-test".into(),
                acquire_timeout: READY_DEADLINE,
            },
        )?)
    }

    fn database_url(&self) -> String {
        format!(
            "{}?options=-c%20search_path%3D{}",
            self.admin_url, self.schema
        )
    }

    async fn bind_foreign_epoch(&self) -> anyhow::Result<()> {
        let url = self.admin_url.clone();
        let schema = self.schema.clone();
        tokio::task::spawn_blocking(move || {
            let mut admin = gobby_core::postgres::connect_readwrite(&url)?;
            admin.execute(
                &format!(
                    "INSERT INTO {schema}.terminals VALUES
                     ('M2', '00000000-0000-0000-0000-000000000001', $1, 'live', 'native')"
                ),
                &[&"epoch-E"],
            )?;
            anyhow::Ok(())
        })
        .await??;
        Ok(())
    }
}

impl Drop for OwnedHubSchema {
    fn drop(&mut self) {
        let url = self.admin_url.clone();
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
                .expect("isolated schema cleanup thread")
                .expect("isolated schema cleanup");
        }
    }
}

#[tokio::test]
async fn postgres_epoch_bindings_drive_adoption_and_refusal() -> anyhow::Result<()> {
    let fixture = OwnedHubSchema::new().await?;
    let authority = Arc::new(PostgresEpochAuthority::new(fixture.pool()?));
    let bindings = authority.bindings("epoch-E").await?;
    assert_eq!(bindings.len(), 2);
    assert!(
        bindings
            .iter()
            .all(|row| row.machine_id == "10000000-0000-4000-8000-000000000001")
    );
    assert!(
        bindings
            .iter()
            .any(|row| row.state == "live"
                && row.project_id == "00000000-0000-0000-0000-000000000001")
    );
    assert!(bindings.iter().any(|row| row.state == "orphaned"));
    assert!(authority.bindings("unbound-epoch").await?.is_empty());

    let mut host = HostFixture::new(None, 1, false).await;
    let supervisor = HostSupervisor::new(host.options(), authority.clone());
    assert_eq!(supervisor.start().await?.epoch, "epoch-E");
    supervisor.stop().await;
    fixture.bind_foreign_epoch().await?;
    let supervisor = HostSupervisor::new(host.options(), authority);
    assert_eq!(supervisor.start().await, Err(HostFailure::EpochRefused));
    assert!(supervisor.retry_armed());
    supervisor.stop().await;
    assert!(host.alive());
    assert_eq!(host.connections.load(Ordering::SeqCst), 2);
    Ok(())
}

#[tokio::test]
async fn shutdown_cancels_inflight_start_callers() {
    let mut host = HostFixture::new(Some("stall"), 1, false).await;
    let supervisor = Arc::new(HostSupervisor::new(host.options(), Arc::new(Rows(vec![]))));
    let first = {
        let owner = supervisor.clone();
        tokio::spawn(async move { owner.start().await })
    };
    let second = {
        let owner = supervisor.clone();
        tokio::spawn(async move { owner.start().await })
    };
    tokio::time::timeout(Duration::from_secs(2), async {
        while host.connections.load(Ordering::SeqCst) == 0 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("start reached handshake");
    supervisor.stop().await;
    assert_eq!(first.await.unwrap(), Err(HostFailure::Stopped));
    assert_eq!(second.await.unwrap(), Err(HostFailure::Stopped));
    assert!(!supervisor.retry_armed());
    assert_eq!(*supervisor.subscribe().borrow(), HostStatus::Stopped);
    assert_eq!(host.connections.load(Ordering::SeqCst), 1);
    assert!(host.alive());
}

#[tokio::test]
async fn health_pidfile_io_keeps_post_adoption_type() {
    let mut host = HostFixture::new(None, 1, false).await;
    let mut options = host.options();
    options.health_interval = Duration::from_millis(50);
    let supervisor = HostSupervisor::new(options, Arc::new(Rows(vec![])));
    let mut status = supervisor.subscribe();
    supervisor.start().await.unwrap();
    tokio::fs::remove_file(host.dir.path().join("gterm.pid"))
        .await
        .unwrap();
    let failure = tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if let HostStatus::Unavailable(failure) = status.borrow_and_update().clone() {
                return failure;
            }
            status.changed().await.unwrap();
        }
    })
    .await
    .unwrap();
    assert!(
        matches!(failure, HostFailure::PostAdoptionIo(_)),
        "health pidfile I/O follows coherent adoption: {failure:?}"
    );
    supervisor.stop().await;
    assert!(host.alive(), "health identity loss must preserve the host");
}

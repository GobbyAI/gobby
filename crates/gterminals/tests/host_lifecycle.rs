//! Lifecycle boundaries use real Unix control sockets and an identity-checked process.
use std::future::Future;
use std::pin::Pin;
use std::process::{Child, Command};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use gobby_core::bootstrap::RouteBackend;
use gobby_terminals::control::{ControlClient, SpawnRequest};
use gobby_terminals::family::{SupervisorMode, TerminalFamily};
use gobby_terminals::frames::{ClientMessage, FrameClient, RenderEncoding, ServerMessage};
use gobby_terminals::host::PostgresEpochAuthority;
use gobby_terminals::host::READY_DEADLINE;
use gobby_terminals::host::{
    EpochAuthority, EpochBinding, HostFailure, HostOptions, HostStatus, HostSupervisor,
};
use serde_json::{Value, json};
use tempfile::TempDir;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
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
            machine_id: "M1".into(),
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
        Arc::new(Rows(vec![binding("M1", "epoch-E")])),
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
        let family = TerminalFamily::new(route, host.options(), Arc::new(Rows(vec![])));
        assert_eq!(family.mode(), SupervisorMode::Observer);
        assert!(family.start().await.unwrap().is_none());
        family.stop().await;
    }
    assert_eq!(host.connections.load(Ordering::SeqCst), 0);
    let family = TerminalFamily::new(RouteBackend::Native, host.options(), Arc::new(Rows(vec![])));
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
        machine_id: "M1".into(),
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
                     ('M1', '00000000-0000-0000-0000-000000000001', 'epoch-E', 'live', 'native'),
                     ('M1', '00000000-0000-0000-0000-000000000002', 'epoch-E', 'orphaned', 'native'),
                     ('M2', '00000000-0000-0000-0000-000000000001', 'other-epoch', 'live', 'native'),
                     ('M2', '00000000-0000-0000-0000-000000000001', NULL, 'live', 'tmux');"
            ))?;
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

    async fn configure_host(&self, home: &std::path::Path, binary: &str) -> anyhow::Result<()> {
        let url = self.admin_url.clone();
        let schema = self.schema.clone();
        let settings = [
            ("terminal_host.socket_dir", json!(home)),
            ("terminal_host.binary_path", json!(binary)),
            ("terminal_host.health_interval_seconds", json!(0.2)),
            ("terminal_host.shutdown_grace_seconds", json!(0.1)),
        ];
        tokio::task::spawn_blocking(move || {
            let mut admin = gobby_core::postgres::connect_readwrite(&url)?;
            for (key, value) in settings {
                admin.execute(
                    &format!("INSERT INTO {schema}.config_store (key, value) VALUES ($1, $2)"),
                    &[&key, &value.to_string()],
                )?;
            }
            anyhow::Ok(())
        })
        .await??;
        Ok(())
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
    assert!(bindings.iter().all(|row| row.machine_id == "M1"));
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

struct RealDaemon {
    home: TempDir,
    binary: String,
    http_port: u16,
    child: Option<Child>,
}

impl RealDaemon {
    async fn start(&mut self) -> anyhow::Result<()> {
        self.start_with_parent(false).await
    }

    async fn start_with_parent(&mut self, parent: bool) -> anyhow::Result<()> {
        let log = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.home.path().join("gdaemon.log"))?;
        let mut command = Command::new(&self.binary);
        command
            .arg("serve")
            .env("GOBBY_HOME", self.home.path())
            .env(
                "GOBBY_FRONT_DOOR_SECRET",
                "isolated-terminal-restart-secret",
            )
            .stdout(log.try_clone()?)
            .stderr(log);
        if parent {
            command
                .env("GOBBY_PARENT_FD", "0")
                .stdin(std::process::Stdio::piped());
        } else {
            command.env_remove("GOBBY_PARENT_FD");
        }
        self.child = Some(command.spawn()?);
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        while tokio::time::Instant::now() < deadline {
            let probe = tokio::time::timeout(Duration::from_millis(250), async {
                let mut stream =
                    tokio::net::TcpStream::connect(("127.0.0.1", self.http_port)).await?;
                stream
                    .write_all(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                    .await?;
                let mut byte = [0u8; 1];
                stream.read_exact(&mut byte).await?;
                anyhow::Ok(())
            })
            .await;
            if matches!(probe, Ok(Ok(()))) {
                return Ok(());
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        anyhow::bail!(
            "isolated gdaemon did not serve: {}",
            std::fs::read_to_string(self.home.path().join("gdaemon.log"))?
        );
    }

    async fn stop(&mut self) -> anyhow::Result<()> {
        let child = self.child.as_mut().expect("running isolated daemon");
        assert!(
            Command::new("/bin/kill")
                .args(["-TERM", &child.id().to_string()])
                .status()?
                .success()
        );
        let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
        loop {
            if let Some(status) = child.try_wait()? {
                assert!(status.success(), "ordinary daemon stop failed: {status}");
                self.child = None;
                return Ok(());
            }
            anyhow::ensure!(
                tokio::time::Instant::now() < deadline,
                "isolated daemon ignored ordinary SIGTERM"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    async fn parent_stop(&mut self, drain: bool) -> anyhow::Result<()> {
        use std::io::Write;
        let child = self.child.as_mut().expect("running isolated daemon");
        let mut pipe = child.stdin.take().expect("parent pipe");
        if drain {
            pipe.write_all(b"D\n")?;
        }
        drop(pipe);
        let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(status) = child.try_wait()? {
                assert!(
                    status.success(),
                    "parent shutdown failed: {status}: {}",
                    std::fs::read_to_string(self.home.path().join("gdaemon.log"))?
                );
                self.child = None;
                return Ok(());
            }
            anyhow::ensure!(
                tokio::time::Instant::now() < deadline,
                "daemon ignored parent shutdown"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }
}

impl Drop for RealDaemon {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        // Both this pidfile and the host it names belong to this test's temporary home.
        if let Ok(pid) = std::fs::read_to_string(self.home.path().join("gterm.pid"))
            && pid.trim().parse::<u32>().is_ok()
        {
            let identity = Command::new("/bin/ps")
                .args(["-p", pid.trim(), "-o", "comm="])
                .output();
            if identity.is_ok_and(|output| {
                output.status.success()
                    && std::path::Path::new(String::from_utf8_lossy(&output.stdout).trim())
                        .file_name()
                        .is_some_and(|name| name == "gterm")
            }) {
                let _ = Command::new("/bin/kill")
                    .args(["-KILL", pid.trim()])
                    .output();
            }
        }
    }
}

fn isolated_public_port() -> anyhow::Result<u16> {
    for _ in 0..32 {
        let public = std::net::TcpListener::bind("127.0.0.1:0")?;
        let port = public.local_addr()?.port();
        if port < 65000
            && std::net::TcpListener::bind(("127.0.0.1", port + 1)).is_ok()
            && std::net::TcpListener::bind(("127.0.0.1", port + 100)).is_ok()
        {
            return Ok(port);
        }
    }
    anyhow::bail!("no isolated front-door port and backend pair available")
}

async fn control_at(
    home: &std::path::Path,
) -> anyhow::Result<ControlClient<tokio::net::UnixStream>> {
    let token = std::fs::read_to_string(home.join("gterm-control.token"))?;
    let stream = tokio::net::UnixStream::connect(home.join("gterm-control.sock")).await?;
    let mut client = ControlClient::new(stream);
    client.hello(token.trim()).await?;
    Ok(client)
}

async fn increment_terminal(
    frames: &mut FrameClient<tokio::net::UnixStream>,
    expected: u32,
) -> anyhow::Result<()> {
    // Octal escapes keep expected output out of the terminal's input echo.
    frames.send(&ClientMessage::Input {
        data: b"n=${n:-0}; n=$((n + 1)); printf '\\120\\122\\105\\123\\105\\122\\126\\105\\104_%s\\n' \"$n\"\n".to_vec(),
    }).await?;
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            match frames.recv().await? {
                ServerMessage::Frame(frame) => {
                    let text: String = frame
                        .cells
                        .iter()
                        .map(|cell| cell.symbol.as_str())
                        .collect();
                    if text.contains(&format!("PRESERVED_{expected}")) {
                        return anyhow::Ok(());
                    }
                }
                ServerMessage::InputRefused { code } | ServerMessage::Error { code, .. } => {
                    anyhow::bail!("isolated terminal I/O refused: {code}");
                }
                ServerMessage::TerminalExited { .. } => anyhow::bail!("live terminal exited"),
                _ => {}
            }
        }
    })
    .await??;
    Ok(())
}

async fn isolated_daemon(hub: &OwnedHubSchema) -> anyhow::Result<RealDaemon> {
    let home = TempDir::new()?;
    let binary = std::env::var("GOBBY_TERMINALS_TEST_GDAEMON")?;
    let gterm = std::env::var("GOBBY_TERMINALS_TEST_GTERM")?;
    hub.configure_host(home.path(), &gterm).await?;
    let http_port = isolated_public_port()?;
    let mut ws_port = isolated_public_port()?;
    while ws_port.abs_diff(http_port) <= 101 {
        ws_port = isolated_public_port()?;
    }
    std::fs::write(home.path().join("machine_id"), "isolated-restart-machine")?;
    let bootstrap = home.path().join("bootstrap.yaml");
    std::fs::write(
        &bootstrap,
        json!({"database_url": hub.database_url(),
        "api_key": "isolated-frame-token", "bind_host": "127.0.0.1",
        "daemon_port": http_port, "websocket_port": ws_port,
        "front_door": {"enabled": true, "routes": {"terminal_ws": "native"},
            "tls": {"mode": "off"}}})
        .to_string(),
    )?;
    std::fs::set_permissions(
        &bootstrap,
        <std::fs::Permissions as std::os::unix::fs::PermissionsExt>::from_mode(0o600),
    )?;
    Ok(RealDaemon {
        home,
        binary,
        http_port,
        child: None,
    })
}

#[tokio::test]
async fn daemon_restart_preserves_live_terminal() -> anyhow::Result<()> {
    let hub = OwnedHubSchema::new().await?;
    let mut daemon = isolated_daemon(&hub).await?;
    daemon.start().await?;
    let pidfile = daemon.home.path().join("gterm.pid");
    assert!(
        pidfile.exists(),
        "native gdaemon must supervise from confirmed absence: {}",
        std::fs::read_to_string(daemon.home.path().join("gdaemon.log"))?
    );
    daemon.stop().await?;
    let mut control = control_at(daemon.home.path()).await?;
    let original = control.ping().await?;
    let terminal_id = uuid::Uuid::new_v4().to_string();
    let spawn_key = uuid::Uuid::new_v4().to_string();
    let reserve_key = uuid::Uuid::new_v4().to_string();
    let reservation = control.reserve_observer(&terminal_id, &reserve_key).await?;
    let reservation_id = reservation["reservation_id"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("host did not reserve an observer"))?
        .to_owned();
    let prepared = control
        .spawn(&SpawnRequest {
            terminal_id: terminal_id.clone(),
            spawn_key: spawn_key.clone(),
            argv: vec!["/bin/sh".into()],
            cwd: daemon.home.path().to_string_lossy().into_owned(),
            env: [
                ("TERM".into(), "xterm-256color".into()),
                ("PS1".into(), "".into()),
            ]
            .into(),
            cols: 100,
            rows: 24,
            commit_deadline_ms: 30_000,
            reservation_id: Some(reservation_id),
            reserve_key: Some(reserve_key),
        })
        .await?;
    control
        .spawn_commit(&terminal_id, &spawn_key, Some(30_000))
        .await?;
    drop(control);
    let attachment_id = uuid::Uuid::new_v4().to_string();
    // This existing control verb is outside the public client's current scope.
    // Exercise it through the unchanged JSON-lines wire in the test fixture.
    {
        use tokio::io::{AsyncBufReadExt, BufReader};
        let token = std::fs::read_to_string(daemon.home.path().join("gterm-control.token"))?;
        let stream =
            tokio::net::UnixStream::connect(daemon.home.path().join("gterm-control.sock")).await?;
        let mut stream = BufReader::new(stream);
        for request in [
            json!({"method": "hello", "id": "1", "protocol_version": 1, "control_token": token.trim()}),
            json!({"method": "grant_input", "id": "2", "operation_seq": 1,
                "host_terminal_id": prepared.host_terminal_id, "attachment_id": attachment_id}),
        ] {
            stream
                .get_mut()
                .write_all(format!("{request}\n").as_bytes())
                .await?;
            let mut reply = String::new();
            tokio::time::timeout(Duration::from_secs(2), stream.read_line(&mut reply)).await??;
            let reply: serde_json::Value = serde_json::from_str(&reply)?;
            assert_eq!(reply["ok"], true, "input grant fixture: {reply}");
        }
    }
    let stream =
        tokio::net::UnixStream::connect(daemon.home.path().join("gterm-frames.sock")).await?;
    let mut frames = FrameClient::new(stream);
    frames
        .handshake(
            &original.host_epoch,
            "isolated-frame-token",
            RenderEncoding::SemanticFrame,
            100,
            24,
            None,
        )
        .await?;
    frames
        .send(&ClientMessage::AttachTerminal {
            host_terminal_id: prepared.host_terminal_id,
            reservation_id: None,
            locator: None,
        })
        .await?;
    frames
        .send(&ClientMessage::BindAttachment { attachment_id })
        .await?;
    for counter in 1..=3 {
        daemon.start().await?;
        increment_terminal(&mut frames, counter).await?;
        daemon.stop().await?;
        let mut control = control_at(daemon.home.path()).await?;
        let current = control.ping().await?;
        assert_eq!(current.host_pid, original.host_pid);
        assert_eq!(current.host_epoch, original.host_epoch);
        assert!(
            control
                .list()
                .await?
                .terminals
                .iter()
                .any(|row| row["terminal_id"] == terminal_id)
        );
        drop(control);
    }
    Ok(())
}

#[tokio::test]
async fn explicit_parent_drain_restarts_with_fresh_epoch() -> anyhow::Result<()> {
    let hub = OwnedHubSchema::new().await?;
    let mut daemon = isolated_daemon(&hub).await?;
    daemon.start_with_parent(true).await?;
    daemon.parent_stop(false).await?;
    let mut control = control_at(daemon.home.path()).await?;
    let original = control.ping().await?;
    let terminal_id = uuid::Uuid::new_v4().to_string();
    let spawn_key = uuid::Uuid::new_v4().to_string();
    let reserve_key = uuid::Uuid::new_v4().to_string();
    let reservation = control.reserve_observer(&terminal_id, &reserve_key).await?;
    let prepared = control
        .spawn(&SpawnRequest {
            terminal_id: terminal_id.clone(),
            spawn_key: spawn_key.clone(),
            argv: vec!["/bin/sh".into()],
            cwd: daemon.home.path().to_string_lossy().into_owned(),
            env: [("TERM".into(), "xterm-256color".into())].into(),
            cols: 80,
            rows: 24,
            commit_deadline_ms: 30_000,
            reservation_id: Some(
                reservation["reservation_id"]
                    .as_str()
                    .expect("reservation")
                    .to_owned(),
            ),
            reserve_key: Some(reserve_key),
        })
        .await?;
    control
        .spawn_commit(&terminal_id, &spawn_key, Some(30_000))
        .await?;
    drop(control);
    daemon.start_with_parent(true).await?;
    daemon.parent_stop(true).await?;
    assert!(
        !daemon.home.path().join("gterm.pid").exists(),
        "explicit drain must terminate the host"
    );
    let processes = Command::new("/bin/ps").args(["-axo", "pgid="]).output()?;
    let groups = String::from_utf8_lossy(&processes.stdout);
    assert!(
        !groups
            .lines()
            .any(|group| group.trim() == prepared.pgid.to_string()),
        "drain must reap the native terminal process group"
    );
    daemon.start().await?;
    daemon.stop().await?;
    let mut control = control_at(daemon.home.path()).await?;
    let current = control.ping().await?;
    assert_ne!(current.host_epoch, original.host_epoch);
    assert!(control.list().await?.terminals.is_empty());
    Ok(())
}

#[tokio::test]
async fn spawned_identity_survives_health_and_stop_disarms_retry() -> anyhow::Result<()> {
    let hub = OwnedHubSchema::new().await?;
    let daemon = isolated_daemon(&hub).await?;
    let pool = hub.pool()?;
    let options = HostOptions::from_pool(&pool, daemon.home.path(), "M1".into())
        .await?
        .expect("enabled test host");
    let supervisor = HostSupervisor::new(options, Arc::new(Rows(vec![])));
    let mut status = supervisor.subscribe();
    let original = supervisor.start().await?;
    assert!(
        !original.adopted,
        "confirmed absence must be recorded as a spawn"
    );
    status.borrow_and_update();
    tokio::time::timeout(Duration::from_secs(2), status.changed()).await??;
    let HostStatus::Ready(current) = status.borrow_and_update().clone() else {
        anyhow::bail!("host did not remain healthy");
    };
    assert_eq!(current.pid, original.pid);
    assert_eq!(current.epoch, original.epoch);
    assert!(
        !current.adopted,
        "a health ping must preserve the spawn origin"
    );
    supervisor.stop().await;
    assert!(!supervisor.retry_armed());
    assert_eq!(supervisor.start().await, Err(HostFailure::Stopped));
    let mut control = control_at(daemon.home.path()).await?;
    assert_eq!(control.ping().await?.host_epoch, original.epoch);
    control.host_shutdown(100).await?;
    drop(control);
    tokio::time::timeout(Duration::from_secs(2), async {
        while tokio::fs::try_exists(daemon.home.path().join("gterm.pid")).await? {
            tokio::task::yield_now().await;
        }
        Ok::<(), std::io::Error>(())
    })
    .await??;
    status.borrow_and_update();
    assert!(
        tokio::time::timeout(Duration::from_millis(400), status.changed())
            .await
            .is_err(),
        "stopped supervisor must not publish a new retry or host"
    );
    assert!(
        !daemon.home.path().join("gterm.pid").exists(),
        "stopped supervisor must never respawn"
    );
    assert_eq!(*supervisor.subscribe().borrow(), HostStatus::Stopped);
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

async fn drain_unadoptable_live_host(token_mismatch: bool) -> anyhow::Result<bool> {
    let hub = OwnedHubSchema::new().await?;
    let daemon = isolated_daemon(&hub).await?;
    let options = HostOptions::from_pool(&hub.pool()?, daemon.home.path(), "M1".into())
        .await?
        .expect("enabled test host");
    let owner = HostSupervisor::new(options.clone(), Arc::new(Rows(vec![])));
    let original = owner.start().await?;
    owner.stop().await;
    let rows = if token_mismatch {
        tokio::fs::write(
            options.socket_dir.join("gterm-control.token"),
            "wrong-token",
        )
        .await?;
        vec![]
    } else {
        vec![binding("foreign-machine", &original.epoch)]
    };
    let supervisor = HostSupervisor::new(options, Arc::new(Rows(rows)));
    assert_eq!(
        supervisor.start().await,
        Err(if token_mismatch {
            HostFailure::TokenMismatch
        } else {
            HostFailure::EpochRefused
        })
    );
    let result = supervisor.drain().await;
    assert!(
        result.is_ok(),
        "explicit drain must bypass adoption failure: {result:?}"
    );
    let process = Command::new("/bin/ps")
        .args(["-p", &original.pid.to_string(), "-o", "stat="])
        .output()?;
    let state = String::from_utf8_lossy(&process.stdout);
    Ok(!daemon.home.path().join("gterm.pid").exists()
        && (state.trim().is_empty() || state.trim().starts_with('Z')))
}

#[tokio::test]
async fn explicit_drain_terminates_token_mismatch_host() -> anyhow::Result<()> {
    assert!(
        drain_unadoptable_live_host(true).await?,
        "explicit drain must terminate the token-mismatched host pid"
    );
    Ok(())
}

#[tokio::test]
async fn explicit_drain_terminates_foreign_epoch_host() -> anyhow::Result<()> {
    assert!(
        drain_unadoptable_live_host(false).await?,
        "explicit drain must terminate the foreign-epoch host pid"
    );
    Ok(())
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

//! `gdaemon serve` tests: bootstrap loading, bind addresses, the accept loop and the fd limit.

use super::*;

#[test]
fn disabled_front_door_refuses_with_typed_message() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("bootstrap.yaml");
    std::fs::write(&path, "front_door:\n  enabled: false\n").expect("write bootstrap");

    let error = load_enabled_bootstrap(&path).expect_err("disabled front door must refuse");

    assert!(
        error.to_string().starts_with("front_door_disabled:"),
        "{error}"
    );
}

#[tokio::test]
async fn bind_host_family_matches_the_python_backend() -> Result<()> {
    let localhost = bind_addr("localhost", 60887).await?;
    let ipv6 = bind_addr("::1", 60887).await?;

    assert_eq!(localhost, SocketAddr::from(([127, 0, 0, 1], 60887)));
    assert!(ipv6.is_ipv6());
    Ok(())
}

#[test]
fn missing_bootstrap_serves_default_ports() {
    let dir = tempfile::tempdir().expect("tempdir");

    let bootstrap = load_enabled_bootstrap(&dir.path().join("bootstrap.yaml")).expect("defaults");

    assert_eq!(
        (bootstrap.daemon_port, bootstrap.websocket_port),
        (DEFAULT_DAEMON_PORT, DEFAULT_WEBSOCKET_PORT)
    );
    assert!(bootstrap.front_door.enabled);
}

/// A listener whose first `emfiles` accepts fail with EMFILE.
#[cfg(unix)]
struct ExhaustedListener {
    listener: TcpListener,
    emfiles: std::sync::Mutex<usize>,
}

#[cfg(unix)]
impl AcceptSource for ExhaustedListener {
    fn accept(&self) -> impl Future<Output = std::io::Result<(TcpStream, SocketAddr)>> + Send {
        let exhausted = {
            let mut left = self.emfiles.lock().expect("fixture exhaustion counter");
            let exhausted = *left > 0;
            *left = left.saturating_sub(1);
            exhausted
        };
        async move {
            if exhausted {
                return Err(std::io::Error::from_raw_os_error(libc::EMFILE));
            }
            self.listener.accept().await
        }
    }
}

#[cfg(unix)]
#[tokio::test]
async fn accept_loop_serves_after_transient_emfile() -> Result<()> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let listener = TcpListener::bind("127.0.0.1:0").await?;
    let addr = listener.local_addr()?;
    let auth = Arc::new(AuthState::new(
        "test-secret".into(),
        None,
        Default::default(),
    )?);
    let state = FrontDoorState::new("127.0.0.1:1".parse()?, BackendState::Down, auth);
    let table = RouteTable::new(FAMILIES, &BTreeMap::new(), &state);
    let exhausted = ExhaustedListener {
        listener,
        emfiles: 2.into(),
    };
    let mut accept_loop = tokio::spawn(accept(exhausted, FrontDoor::new(state, table), None));

    let round_trip = async {
        let mut client = TcpStream::connect(addr).await?;
        client
            .write_all(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            .await?;
        let mut response = String::new();
        client.read_to_string(&mut response).await?;
        anyhow::Ok(response)
    };
    let response = tokio::select! {
        biased;
        finished = &mut accept_loop => panic!("accept loop exited on EMFILE: {finished:?}"),
        response = tokio::time::timeout(Duration::from_secs(10), round_trip) => response??,
    };

    assert!(response.starts_with("HTTP/1.1 "), "{response}");
    accept_loop.abort();
    Ok(())
}

/// Set in the child process of the routed activation test, to its logs directory.
#[cfg(unix)]
const ROUTED_START_CHILD: &str = "GOBBY_SERVE_TEST_ROUTED_START_CHILD";

/// The start's unreadable-configuration warning.
#[cfg(unix)]
const UNREADABLE_AT_START: &str = "the active configuration is unreadable at start";

/// The degraded fixture family's start warning.
#[cfg(unix)]
const STARTED_DEGRADED: &str = "family fixture_starts_degraded started degraded";

/// An epoch source whose reads fail until it is made readable.
#[cfg(unix)]
#[derive(Default)]
struct UnreadableUntilSet {
    readable: std::sync::atomic::AtomicBool,
}

#[cfg(unix)]
impl crate::family_start::ConfigEpochSource for UnreadableUntilSet {
    fn read(
        &self,
    ) -> std::pin::Pin<
        Box<dyn Future<Output = anyhow::Result<gobby_core::family::ConfigEpoch>> + Send + '_>,
    > {
        let readable = self.readable.load(std::sync::atomic::Ordering::SeqCst);
        Box::pin(async move {
            anyhow::ensure!(readable, "the fixture's configuration store is unreachable");
            Ok(gobby_core::family::ConfigEpoch::unpublished())
        })
    }
}

/// A frame publisher with no subscribers.
#[cfg(unix)]
struct NoFrames;

#[cfg(unix)]
impl gobby_core::family::FramePublisher for NoFrames {
    fn publish_frames(
        &self,
        _filter: &serde_json::Value,
        _frames: Vec<String>,
    ) -> Result<(), gobby_core::family::PublishRefused> {
        Err(gobby_core::family::PublishRefused)
    }
}

/// A family whose start fails without stopping the phase, so it runs degraded.
#[cfg(unix)]
struct StartsDegraded;

#[cfg(unix)]
impl RouteFamily<FrontDoorState> for StartsDegraded {
    fn name(&self) -> &'static str {
        "fixture_starts_degraded"
    }

    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }

    fn router(&self) -> axum::Router<FrontDoorState> {
        axum::Router::new()
    }

    fn start<'a>(
        &'a self,
        _ctx: &'a FamilyContext,
    ) -> gobby_core::route_family::BoxFut<'a, Result<(), gobby_core::family::lifecycle::HookError>>
    {
        Box::pin(async {
            Err(gobby_core::family::lifecycle::HookError::Ordinary {
                message: "the fixture's store is unavailable".into(),
            })
        })
    }
}

/// The files under `dir`, or none when it does not exist.
#[cfg(unix)]
fn files_in(dir: &Path) -> Vec<std::path::PathBuf> {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut files: Vec<_> = entries.flatten().map(|entry| entry.path()).collect();
    files.sort();
    files
}

/// Lets paused time run forward by `span`, so every timer inside it fires.
#[cfg(unix)]
async fn let_time_pass(span: Duration) {
    let _ = tokio::time::timeout(span, std::future::pending::<()>()).await;
}

/// Child: a start whose epoch read fails and whose one family starts degraded, through the
/// serve path's routed activation and configuration poller, under paused time.
#[cfg(unix)]
async fn start_with_an_unreadable_configuration(logs: std::path::PathBuf) {
    const EVERY: Duration = Duration::from_secs(1);
    crate::logging::install_pre_config();
    let source = Arc::new(UnreadableUntilSet::default());
    let degraded: &'static StartsDegraded = Box::leak(Box::new(StartsDegraded));
    let families: Vec<&'static dyn RouteFamily<FrontDoorState>> = vec![degraded];
    let started = crate::family_start::prepare(crate::family_start::PrepareInputs {
        serve_pool: None,
        home: std::path::PathBuf::from("/nonexistent/gobby-home"),
        routes: BTreeMap::from([(degraded.name().to_owned(), RouteBackend::Native)]),
        publisher: Arc::new(NoFrames),
        backend: None,
        lease: None,
        epoch_source: source.clone(),
        cpu_count: 8,
        families,
        extensions: Extensions::new(),
        startup: None,
    })
    .await
    .expect("a degraded family and an unreadable configuration do not fail the start");
    assert!(!started.config_read, "the start's epoch read failed");

    let routed = RoutedActivation {
        settings: crate::logging::LogSettings::defaults(logs.clone()),
        backend_command: "fixture-backend".to_owned(),
        start_warnings: started.start_warnings.clone(),
        config: started.ctx.subscribe_config(),
    };
    let (applier, first_read) = routed
        .start(started.config_read)
        .await
        .expect("a start that waits for its first read returns at once");
    let poller = crate::family_start::spawn_config_poller(
        source.clone(),
        started.ctx.config_publisher(),
        EVERY,
        first_read,
    );

    let_time_pass(EVERY * 5).await;
    let before_read = files_in(&logs);
    source
        .readable
        .store(true, std::sync::atomic::Ordering::SeqCst);
    let_time_pass(EVERY * 2).await;
    let after_read = files_in(&logs);
    poller.abort();
    if let Some(applier) = &applier {
        applier.abort();
    }

    assert_eq!(
        before_read,
        Vec::<std::path::PathBuf>::new(),
        "a routed log file exists before the first successful configuration read"
    );
    for name in ["daemon.log", "errors.log"] {
        assert!(
            after_read.contains(&logs.join(name)),
            "{name} is missing after the first successful read: {after_read:?}"
        );
    }
}

#[cfg(unix)]
#[test]
fn an_unreadable_start_writes_no_routed_file_until_a_read_succeeds_then_replays_its_warnings_once()
{
    if let Some(logs) = std::env::var_os(ROUTED_START_CHILD) {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .start_paused(true)
            .build()
            .expect("child runtime")
            .block_on(start_with_an_unreadable_configuration(logs.into()));
        return;
    }

    let home = tempfile::tempdir().expect("temporary logs directory");
    let logs = home.path().join("logs");
    let output = std::process::Command::new(std::env::current_exe().expect("test binary path"))
        .args([
            "--exact",
            "serve::tests::an_unreadable_start_writes_no_routed_file_until_a_read_succeeds_then_replays_its_warnings_once",
            "--nocapture",
        ])
        .env(ROUTED_START_CHILD, &logs)
        .output()
        .expect("child test runs");

    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(output.status.success(), "{:?}: {stderr}", output.status);
    let errors = std::fs::read_to_string(logs.join("errors.log")).unwrap_or_default();
    for warning in [UNREADABLE_AT_START, STARTED_DEGRADED] {
        assert_eq!(
            errors.matches(warning).count(),
            1,
            "the start warning {warning:?} is not in errors.log exactly once: {errors:?}"
        );
        assert_eq!(
            stderr.matches(warning).count(),
            1,
            "the start warning {warning:?} did not reach stderr once before activation: \
             {stderr:?}"
        );
    }
}

#[cfg(unix)]
#[test]
fn serve_startup_raises_the_launchd_fd_soft_limit() {
    let mut limit = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    // SAFETY: `limit` is a valid, writable rlimit for getrlimit to fill.
    assert_eq!(
        unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) },
        0
    );
    assert!(
        limit.rlim_max > 256,
        "hard limit {} leaves no room",
        limit.rlim_max
    );
    limit.rlim_cur = 256;
    // SAFETY: `limit` is initialized and lowers only the soft value, which is
    // always permitted; nextest runs this test in its own process.
    assert_eq!(unsafe { libc::setrlimit(libc::RLIMIT_NOFILE, &limit) }, 0);

    let (before, after) = raise_nofile_limit(NOFILE_TARGET).expect("raise fd limit");

    // SAFETY: as above for getrlimit.
    assert_eq!(
        unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) },
        0
    );
    assert_eq!(before, 256);
    assert_eq!(after, NOFILE_TARGET.min(limit.rlim_max));
    assert_eq!(limit.rlim_cur, after);
    assert!(
        limit.rlim_cur > 256,
        "soft limit stayed at {}",
        limit.rlim_cur
    );
}

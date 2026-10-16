//! `gdaemon serve`: bind the public HTTP and WS ports and run the front door.

use std::collections::BTreeMap;
use std::convert::Infallible;
use std::future::Future;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use axum::body::Body;
use gobby_core::bootstrap::{
    DEFAULT_BIND_HOST, DEFAULT_DAEMON_PORT, DEFAULT_WEBSOCKET_PORT, FrontDoorBootstrap,
    HubDatabaseBootstrap, RouteBackend, read_hub_database_bootstrap_file,
};
#[cfg(unix)]
use gobby_core::bootstrap::{RunMode, backend_ports, bootstrap_path};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::{TcpListener, TcpStream};
use tokio::task::JoinSet;
use tokio_rustls::TlsAcceptor;
use tokio_rustls::rustls::ServerConfig;

use crate::front_door::auth::AuthState;
#[cfg(unix)]
use crate::front_door::auth::{KeyResolver, PostgresKeyResolver};
use crate::front_door::health::BackendState;
use crate::front_door::routes::{FAMILIES, RouteTable};
#[cfg(unix)]
use crate::front_door::routes::{terminal_route_latch, unimplemented_families};
#[cfg(unix)]
use crate::front_door::tls;
use crate::front_door::{FrontDoor, FrontDoorState};
use crate::nodes::NativeRoutes;

/// How long an accepted connection may take to send its first byte and, for
/// TLS, finish the handshake before it is dropped.
const PREAUTH_DEADLINE: Duration = Duration::from_secs(10);

/// First byte of a TLS record carrying a handshake (a ClientHello).
const TLS_HANDSHAKE: u8 = 0x16;

/// Soft `RLIMIT_NOFILE` target, matching the Python runner's `_raise_fd_limit`.
#[cfg(unix)]
const NOFILE_TARGET: libc::rlim_t = 10240;

/// Raise the soft `RLIMIT_NOFILE` toward `target`, capped by the hard limit.
/// launchd starts the front door at 256 and every proxied connection holds two
/// descriptors. Returns the soft limit before and after.
#[cfg(unix)]
fn raise_nofile_limit(target: libc::rlim_t) -> std::io::Result<(libc::rlim_t, libc::rlim_t)> {
    let mut limit = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    // SAFETY: `limit` is a valid, writable rlimit for getrlimit to fill.
    if unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    let before = limit.rlim_cur;
    // RLIM_INFINITY is the largest rlim_t, so `min` also covers an unlimited hard limit.
    let raised = target.min(limit.rlim_max);
    if before >= raised {
        return Ok((before, before));
    }
    limit.rlim_cur = raised;
    // SAFETY: `limit` is an initialized rlimit whose soft value does not exceed its hard value.
    if unsafe { libc::setrlimit(libc::RLIMIT_NOFILE, &limit) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok((before, raised))
}

/// Pause after a transient accept failure so descriptors or buffers can drain.
const ACCEPT_BACKOFF: Duration = Duration::from_millis(50);

/// Minimum spacing between transient accept-failure warnings.
const ACCEPT_WARNING_INTERVAL: Duration = Duration::from_secs(10);

/// Accept failures caused by resource exhaustion or one aborted connection; the
/// listener itself is still usable, so the loop backs off and keeps accepting.
fn transient_accept_error(error: &std::io::Error) -> bool {
    #[cfg(unix)]
    if matches!(
        error.raw_os_error(),
        Some(libc::EMFILE | libc::ENFILE | libc::ENOBUFS)
    ) {
        return true;
    }
    matches!(
        error.kind(),
        std::io::ErrorKind::ConnectionAborted | std::io::ErrorKind::OutOfMemory
    )
}

/// The accept half of a listener; tests substitute one that injects errors.
trait AcceptSource: Send + 'static {
    fn accept(&self) -> impl Future<Output = std::io::Result<(TcpStream, SocketAddr)>> + Send;
}

impl AcceptSource for TcpListener {
    fn accept(&self) -> impl Future<Output = std::io::Result<(TcpStream, SocketAddr)>> + Send {
        TcpListener::accept(self)
    }
}

/// A bound public listener and the loopback backend it proxies to.
pub struct PublicListener {
    pub listener: TcpListener,
    pub backend: SocketAddr,
}

/// Entry point for `gdaemon serve`. Builds a tokio runtime for this subcommand only.
#[cfg(unix)]
pub fn run() -> Result<()> {
    use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
    use gobby_core::postgres_pool::{Pool, PoolSettings};
    use rand::RngCore;
    let path = bootstrap_path().context("cannot resolve the Gobby home directory")?;
    let bootstrap = load_enabled_bootstrap(&path)?;
    match raise_nofile_limit(NOFILE_TARGET) {
        Ok((before, after)) => eprintln!("gdaemon serve: fd soft limit {before} -> {after}"),
        Err(error) => {
            eprintln!("gdaemon serve: warning: could not raise the fd soft limit: {error}")
        }
    }
    let native = if bootstrap.mode == RunMode::Node {
        None
    } else {
        let mut secret = [0u8; 32];
        rand::thread_rng().fill_bytes(&mut secret);
        let secret = URL_SAFE_NO_PAD.encode(secret);
        // One pool serves key resolution, the lease and the hub node routes.
        let pool = bootstrap
            .database_url
            .as_deref()
            .map(|url| {
                Pool::build(
                    url,
                    PoolSettings {
                        max_size: 8,
                        application_name: "gobby-gdaemon".into(),
                        acquire_timeout: Duration::from_secs(5),
                    },
                )
                .map(Arc::new)
            })
            .transpose()?;
        let resolver = pool
            .clone()
            .map(|pool| Arc::new(PostgresKeyResolver::new(pool)) as Arc<dyn KeyResolver>);
        let auth = Arc::new(AuthState::new(secret.clone(), resolver, path.clone())?);
        let ports = backend_ports(bootstrap.daemon_port, bootstrap.websocket_port)?;
        for name in unimplemented_families(FAMILIES, &bootstrap.front_door.routes) {
            eprintln!(
                "gdaemon serve: front_door.routes.{name} requests a native backend gdaemon does not implement; proxying"
            );
        }
        let terminals = terminal_route_latch(&bootstrap.front_door.routes)?;
        Some((secret, auth, ports, pool, terminals))
    };
    let tls = tls::load(&bootstrap.front_door.tls, &bootstrap.bind_host)?;
    if let Some(loaded) = &tls {
        for name in &loaded.missing_sans {
            eprintln!(
                "gdaemon serve: front_door.tls.sans entry {name} is missing from the existing certificate; \
                 remove both certificate files and restart to regenerate (nodes must log in again)"
            );
        }
        eprintln!("front door certificate {}", loaded.fingerprint);
    }

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .context("failed to start the tokio runtime")?;
    runtime.block_on(async {
        let home = path.parent().context("bootstrap has no parent directory")?;
        let Some((secret, auth, (backend_http, backend_ws), pool, terminals)) = native else {
            let Some((node, claim)) = crate::state::NodeServices::prepare(&bootstrap, &path, home)?
            else {
                return Ok(());
            };
            let http = bind_listeners(&bootstrap.bind_host, bootstrap.daemon_port).await?;
            let ws = bind_listeners(&bootstrap.bind_host, bootstrap.websocket_port).await?;
            let doors = http
                .into_iter()
                .map(|listener| (listener, FrontDoor::node(node.clone(), false)))
                .chain(
                    ws.into_iter()
                        .map(|listener| (listener, FrontDoor::node(node.clone(), true))),
                )
                .collect();
            let channel = tokio::spawn(crate::nodes::channel::run_client(
                node.clone(),
                crate::nodes::channel::ChannelTiming::default(),
            ));
            let result =
                serve_doors(doors, tls.map(|loaded| loaded.config), shutdown_signal()).await;
            channel.abort();
            let _ = channel.await;
            drop(claim);
            return result;
        };
        let Some((state, supervisor)) =
            crate::state::RuntimeState::prepare(&bootstrap, home, &secret, pool.clone())?
        else {
            eprintln!(
                "gdaemon serve: runtime singleton claim unavailable \
                 (another owner or live service reservation): {}",
                home.join("gobby.pid").display()
            );
            return Ok(());
        };
        let pool = pool.context("backend requires database_url")?;
        let native_routes = if bootstrap.mode == RunMode::Hub {
            NativeRoutes::Hub(Arc::new(crate::nodes::HubNodes {
                db: pool,
                registry: Default::default(),
                timing: Default::default(),
            }))
        } else {
            NativeRoutes::Standalone(Some(pool))
        };
        let mut listeners = bind(&bootstrap.bind_host, bootstrap.daemon_port, backend_http).await?;
        listeners.extend(bind(&bootstrap.bind_host, bootstrap.websocket_port, backend_ws).await?);
        let (stop, shutdown) = tokio::sync::watch::channel(false);
        let owner = state.clone();
        let mut lifecycle = tokio::spawn(owner_run(
            owner,
            supervisor,
            shutdown,
            bootstrap.clone(),
            terminals,
            home.to_owned(),
        ));
        let mut owner_result = None;
        let result = serve_with_runtime(
            listeners,
            &bootstrap.front_door.routes,
            tls.map(|loaded| loaded.config),
            auth,
            Some(state),
            native_routes,
            async {
                tokio::select! {
                    () = shutdown_signal() => {},
                    finished = &mut lifecycle => { owner_result = Some(finished); },
                }
            },
        )
        .await;
        stop.send_replace(true);
        match owner_result {
            Some(finished) => finished,
            None => lifecycle.await,
        }
        .context("runtime owner task failed")??;
        result
    })
}

#[cfg(not(unix))]
pub fn run() -> Result<()> {
    anyhow::bail!(
        "gdaemon serve is unsupported on Windows; launch the Python daemon with gobby start"
    )
}

#[cfg(unix)]
async fn owner_run(
    state: Arc<crate::state::RuntimeState>,
    supervisor: crate::lifecycle::backend::BackendSupervisor,
    shutdown: tokio::sync::watch::Receiver<bool>,
    bootstrap: HubDatabaseBootstrap,
    terminals: gobby_terminals::family::RouteLatch,
    home: std::path::PathBuf,
) -> Result<()> {
    state
        .run(supervisor, shutdown, bootstrap, terminals, home)
        .await
}

/// Load front-door bootstrap; the contained corpus uses the unsupervised entry.
/// when the front door is disabled.
pub fn load_enabled_bootstrap(path: &Path) -> Result<HubDatabaseBootstrap> {
    let bootstrap =
        read_hub_database_bootstrap_file(path)?.unwrap_or_else(|| HubDatabaseBootstrap {
            mode: gobby_core::bootstrap::RunMode::Standalone,
            hub_daemon_url: None,
            hub_cert: None,
            database_url: None,
            api_key: None,
            daemon_url: None,
            bind_host: DEFAULT_BIND_HOST.to_owned(),
            daemon_port: DEFAULT_DAEMON_PORT,
            websocket_port: DEFAULT_WEBSOCKET_PORT,
            front_door: FrontDoorBootstrap::default(),
        });
    if !bootstrap.front_door.enabled {
        anyhow::bail!(
            "front_door_disabled: front_door.enabled is false in {}; Python binds the public ports and gdaemon serve does not run",
            path.display()
        );
    }
    Ok(bootstrap)
}

/// Bind `host:port`, plus a same-port loopback companion when `host` is a
/// concrete non-loopback address, so a local client dialing loopback always
/// reaches the front door.
pub async fn bind(host: &str, port: u16, backend_port: u16) -> Result<Vec<PublicListener>> {
    let backend = SocketAddr::from(([127, 0, 0, 1], backend_port));
    Ok(bind_listeners(host, port)
        .await?
        .into_iter()
        .map(|listener| PublicListener { listener, backend })
        .collect())
}

async fn bind_listeners(host: &str, port: u16) -> Result<Vec<TcpListener>> {
    let listener = TcpListener::bind(bind_addr(host, port).await?)
        .await
        .with_context(|| format!("failed to bind {host}:{port}"))?;
    let bound = listener
        .local_addr()
        .context("bound listener has no address")?;
    let mut listeners = vec![listener];
    if let Some(companion) = companion_addr(bound) {
        let listener = TcpListener::bind(companion)
            .await
            .with_context(|| format!("failed to bind the loopback companion {companion}"))?;
        listeners.push(listener);
    }
    Ok(listeners)
}

/// The loopback address of the same family and port a concrete non-loopback
/// bind adds. A wildcard or loopback bind already accepts loopback and gets none.
pub fn companion_addr(bound: SocketAddr) -> Option<SocketAddr> {
    let ip = bound.ip();
    if ip.is_unspecified() || ip.is_loopback() {
        return None;
    }
    let loopback = match ip {
        IpAddr::V4(_) => IpAddr::V4(Ipv4Addr::LOCALHOST),
        IpAddr::V6(_) => IpAddr::V6(Ipv6Addr::LOCALHOST),
    };
    Some(SocketAddr::new(loopback, bound.port()))
}

/// Whether a connection that did not open with a TLS handshake may be served
/// in plaintext: only from loopback, counting IPv4-mapped IPv6 peers.
pub fn plaintext_allowed(peer: IpAddr) -> bool {
    peer.to_canonical().is_loopback()
}

/// The address `host` binds, chosen as the Python daemon's uvicorn chooses it: an
/// IPv6 literal binds IPv6 and anything else binds IPv4, so `localhost` is
/// `127.0.0.1` even where the resolver lists `::1` first.
async fn bind_addr(host: &str, port: u16) -> Result<SocketAddr> {
    let ipv6 = host.contains(':');
    tokio::net::lookup_host((host, port))
        .await
        .with_context(|| format!("failed to resolve bind host {host}"))?
        .find(|addr| addr.is_ipv6() == ipv6)
        .with_context(|| {
            let family = if ipv6 { "IPv6" } else { "IPv4" };
            format!("bind host {host} has no {family} address")
        })
}

/// Serve every listener until `shutdown` resolves. With `tls`, a connection
/// opening with a TLS handshake is served over TLS from any peer, and any
/// other connection is served in plaintext only to a loopback peer.
pub async fn serve(
    listeners: Vec<PublicListener>,
    routes: &BTreeMap<String, RouteBackend>,
    tls: Option<Arc<ServerConfig>>,
    auth: Arc<AuthState>,
    shutdown: impl Future<Output = ()>,
) -> Result<()> {
    serve_with_runtime(
        listeners,
        routes,
        tls,
        auth,
        #[cfg(unix)]
        None,
        NativeRoutes::default(),
        shutdown,
    )
    .await
}

async fn serve_with_runtime(
    listeners: Vec<PublicListener>,
    routes: &BTreeMap<String, RouteBackend>,
    tls: Option<Arc<ServerConfig>>,
    auth: Arc<AuthState>,
    #[cfg(unix)] runtime: Option<Arc<crate::state::RuntimeState>>,
    native: NativeRoutes,
    shutdown: impl Future<Output = ()>,
) -> Result<()> {
    let mut doors = Vec::with_capacity(listeners.len());
    for PublicListener { listener, backend } in listeners {
        let mut state = FrontDoorState::new(backend, BackendState::Down, auth.clone());
        state.native = native.clone();
        #[cfg(unix)]
        let state = FrontDoorState {
            runtime: runtime.clone(),
            ..state
        };
        let table = RouteTable::new(FAMILIES, routes, &state);
        doors.push((listener, FrontDoor::new(state, table)));
    }
    serve_doors(doors, tls, shutdown).await
}

/// Serve prepared backend or node doors on their public listeners.
pub async fn serve_doors(
    doors: Vec<(TcpListener, FrontDoor)>,
    tls: Option<Arc<ServerConfig>>,
    shutdown: impl Future<Output = ()>,
) -> Result<()> {
    // Stage 1 has no timer-driven Rust jobs yet. Strangler ports register here.
    let mut heartbeat = crate::heartbeat::HeartbeatHost::new();
    heartbeat.start()?;
    let acceptor = tls.map(TlsAcceptor::from);
    let mut accept_loops = JoinSet::new();
    for (listener, door) in doors {
        accept_loops.spawn(accept(listener, door, acceptor.clone()));
    }
    let result = tokio::select! {
        () = shutdown => Ok(()),
        Some(finished) = accept_loops.join_next() => {
            match finished {
                Ok(result) => result,
                Err(error) => Err(error).context("front door accept loop panicked"),
            }
        }
    };
    accept_loops.abort_all();
    heartbeat.stop().await?;
    result
}

/// Accept connections forever. The loop itself never reads: the first-byte
/// peek and TLS handshake run in each connection's own task, under
/// [`PREAUTH_DEADLINE`], so a stalled peer cannot hold up the listener.
async fn accept(
    listener: impl AcceptSource,
    front_door: FrontDoor,
    acceptor: Option<TlsAcceptor>,
) -> Result<()> {
    let mut last_warning: Option<std::time::Instant> = None;
    let mut suppressed = 0u64;
    loop {
        let (stream, peer) = match listener.accept().await {
            Ok(accepted) => accepted,
            Err(error) if transient_accept_error(&error) => {
                if last_warning.is_none_or(|at| at.elapsed() >= ACCEPT_WARNING_INTERVAL) {
                    eprintln!(
                        "gdaemon serve: accept failed, retrying in {}ms \
                         ({suppressed} similar failures suppressed): {error}",
                        ACCEPT_BACKOFF.as_millis()
                    );
                    last_warning = Some(std::time::Instant::now());
                    suppressed = 0;
                } else {
                    suppressed += 1;
                }
                tokio::time::sleep(ACCEPT_BACKOFF).await;
                continue;
            }
            Err(error) => return Err(error).context("accept failed"),
        };
        let front_door = front_door.clone();
        let acceptor = acceptor.clone();
        tokio::spawn(async move {
            let Some(acceptor) = acceptor else {
                return serve_connection(stream, peer, false, front_door).await;
            };
            match tokio::time::timeout(PREAUTH_DEADLINE, preauth(stream, peer, &acceptor)).await {
                Ok(Ok(Preauth::Tls(stream))) => {
                    serve_connection(stream, peer, true, front_door).await
                }
                Ok(Ok(Preauth::Plain(stream))) => {
                    serve_connection(stream, peer, false, front_door).await
                }
                Ok(Ok(Preauth::Refused) | Err(_)) | Err(_) => {}
            }
        });
    }
}

enum Preauth {
    Tls(Box<tokio_rustls::server::TlsStream<TcpStream>>),
    Plain(TcpStream),
    Refused,
}

async fn preauth(
    stream: TcpStream,
    peer: SocketAddr,
    acceptor: &TlsAcceptor,
) -> std::io::Result<Preauth> {
    let mut first = [0_u8; 1];
    if stream.peek(&mut first).await? == 0 {
        return Ok(Preauth::Refused);
    }
    if first[0] == TLS_HANDSHAKE {
        return Ok(Preauth::Tls(Box::new(acceptor.accept(stream).await?)));
    }
    if plaintext_allowed(peer.ip()) {
        Ok(Preauth::Plain(stream))
    } else {
        Ok(Preauth::Refused)
    }
}

async fn serve_connection<S>(stream: S, peer: SocketAddr, https: bool, front_door: FrontDoor)
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let service = service_fn(move |request: hyper::Request<hyper::body::Incoming>| {
        let front_door = front_door.clone();
        async move { Ok::<_, Infallible>(front_door.handle(request.map(Body::new), peer, https).await) }
    });
    let _ = http1::Builder::new()
        .serve_connection(TokioIo::new(stream), service)
        .with_upgrades()
        .await;
}

#[cfg(unix)]
async fn shutdown_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        match signal(SignalKind::terminate()) {
            Ok(mut terminate) => {
                tokio::select! {
                    _ = tokio::signal::ctrl_c() => {}
                    _ = terminate.recv() => {}
                }
            }
            Err(_) => {
                let _ = tokio::signal::ctrl_c().await;
            }
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

#[cfg(test)]
mod tests {
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

        let bootstrap =
            load_enabled_bootstrap(&dir.path().join("bootstrap.yaml")).expect("defaults");

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
        emfiles: std::sync::atomic::AtomicUsize,
    }

    #[cfg(unix)]
    impl AcceptSource for ExhaustedListener {
        fn accept(&self) -> impl Future<Output = std::io::Result<(TcpStream, SocketAddr)>> + Send {
            use std::sync::atomic::Ordering;
            let exhausted = self
                .emfiles
                .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |left| {
                    left.checked_sub(1)
                })
                .is_ok();
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
                .write_all(
                    b"GET /api/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
                )
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
}

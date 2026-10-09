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
    HubDatabaseBootstrap, RouteBackend, backend_ports, bootstrap_path,
    read_hub_database_bootstrap_file,
};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::{TcpListener, TcpStream};
use tokio::task::JoinSet;
use tokio_rustls::TlsAcceptor;
use tokio_rustls::rustls::ServerConfig;

use crate::front_door::auth::{AuthState, KeyResolver, PostgresKeyResolver};
use crate::front_door::health::BackendState;
use crate::front_door::routes::{FAMILIES, RouteTable, unimplemented_families};
use crate::front_door::{FrontDoor, FrontDoorState, tls};

/// How long an accepted connection may take to send its first byte and, for
/// TLS, finish the handshake before it is dropped.
const PREAUTH_DEADLINE: Duration = Duration::from_secs(10);

/// First byte of a TLS record carrying a handshake (a ClientHello).
const TLS_HANDSHAKE: u8 = 0x16;

/// A bound public listener and the loopback backend it proxies to.
pub struct PublicListener {
    pub listener: TcpListener,
    pub backend: SocketAddr,
}

/// Entry point for `gdaemon serve`. Builds a tokio runtime for this subcommand only.
pub fn run() -> Result<()> {
    let secret = std::env::var("GOBBY_FRONT_DOOR_SECRET")
        .ok()
        .filter(|secret| !secret.is_empty())
        .context("GOBBY_FRONT_DOOR_SECRET must be nonempty; launch through the Python runner")?;
    let path = bootstrap_path().context("cannot resolve the Gobby home directory")?;
    let bootstrap = load_enabled_bootstrap(&path)?;
    let resolver = bootstrap
        .database_url
        .as_deref()
        .map(|url| Arc::new(PostgresKeyResolver::new(url)) as Arc<dyn KeyResolver>);
    let auth = Arc::new(AuthState::new(secret, resolver, path.clone())?);
    let (backend_http, backend_ws) =
        backend_ports(bootstrap.daemon_port, bootstrap.websocket_port)?;
    for name in unimplemented_families(FAMILIES, &bootstrap.front_door.routes) {
        eprintln!(
            "gdaemon serve: front_door.routes.{name} requests a native backend gdaemon does not implement; proxying"
        );
    }
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
        let (retention_stop, retention_shutdown) = tokio::sync::watch::channel(false);
        let retention_task = if let Some(database_url) = &bootstrap.database_url {
            let home = path.parent().context("bootstrap has no parent directory")?;
            let machine_id = gobby_core::machine::read_machine_id_from_home(home)?;
            let pool = gobby_core::postgres_pool::Pool::build(
                database_url,
                gobby_core::postgres_pool::PoolSettings {
                    max_size: 2,
                    application_name: "gobby-gdaemon-retention".to_owned(),
                    acquire_timeout: Duration::from_secs(2),
                },
            )?;
            Some(tokio::spawn(crate::retention::run(
                pool,
                uuid::Uuid::parse_str(&machine_id)?,
                retention_shutdown,
            )))
        } else {
            None
        };
        let terminal_family =
            if bootstrap.front_door.routes.get("terminal_ws") == Some(&RouteBackend::Native) {
                let home = path.parent().context("bootstrap has no parent directory")?;
                let pool = gobby_core::postgres_pool::Pool::build(
                    bootstrap
                        .database_url
                        .as_deref()
                        .context("Native terminal supervision requires database_url")?,
                    gobby_core::postgres_pool::PoolSettings {
                        max_size: 8,
                        application_name: "gobby-gdaemon-terminals".to_owned(),
                        acquire_timeout: Duration::from_secs(2),
                    },
                )?;
                let machine_id = gobby_core::machine::read_machine_id_from_home(home)?;
                let options =
                    gobby_terminals::host::HostOptions::from_pool(&pool, home, machine_id).await?;
                options.map(|options| {
                    gobby_terminals::family::TerminalFamily::new(
                        RouteBackend::Native,
                        options,
                        Arc::new(gobby_terminals::host::PostgresEpochAuthority::new(pool)),
                    )
                })
            } else {
                None
            };
        if let Some(family) = &terminal_family
            && let Err(error) = family.start().await
        {
            // Startup settles promptly; the single lifecycle retains bounded retry.
            eprintln!("gdaemon terminal host: {error}");
        }
        let host = bootstrap.bind_host.as_str();
        let mut listeners = bind(host, bootstrap.daemon_port, backend_http).await?;
        listeners.extend(bind(host, bootstrap.websocket_port, backend_ws).await?);
        let parent_gone = watch_parent_fd()?;
        let drain = std::sync::atomic::AtomicBool::new(false);
        let shutdown = async {
            match parent_gone {
                Some(gone) => tokio::select! {
                    () = shutdown_signal() => {}
                    intent = gone => {
                        drain.store(intent.unwrap_or(false), std::sync::atomic::Ordering::Release);
                    }
                },
                None => shutdown_signal().await,
            }
        };
        let tls = tls.map(|loaded| loaded.config);
        let result = serve(listeners, &bootstrap.front_door.routes, tls, auth, shutdown).await;
        retention_stop.send_replace(true);
        if let Some(task) = retention_task {
            task.await.context("token-event retention task failed")?;
        }
        if let Some(family) = &terminal_family {
            if drain.load(std::sync::atomic::Ordering::Acquire) {
                family
                    .drain()
                    .await
                    .context("explicit terminal drain failed")?;
            } else {
                family.stop().await;
            }
        }
        result
    })
}

/// The runner that owns this `serve` passes the read end of a liveness pipe in
/// `GOBBY_PARENT_FD`; the receiver resolves on EOF, meaning the runner is gone by
/// any cause, SIGKILL included. Without the variable there is no watch. The read
/// runs on a detached thread, not the blocking pool, so a runtime drop never
/// waits on it.
fn watch_parent_fd() -> Result<Option<tokio::sync::oneshot::Receiver<bool>>> {
    let Some(value) = std::env::var_os(PARENT_FD_ENV) else {
        return Ok(None);
    };
    let mut pipe = inherited_pipe(&value)?;
    let (gone, watch) = tokio::sync::oneshot::channel();
    std::thread::Builder::new()
        .name("parent-fd-watch".to_owned())
        .spawn(move || {
            use std::io::{ErrorKind, Read};
            let mut buf = [0u8; 64];
            let mut line = Vec::with_capacity(2);
            loop {
                match pipe.read(&mut buf) {
                    Ok(0) => break,
                    Ok(count) => {
                        for byte in &buf[..count] {
                            if *byte == b'\n' {
                                if line == b"D" {
                                    let _ = gone.send(true);
                                    return;
                                }
                                line.clear();
                            } else if line.len() < 2 {
                                line.push(*byte);
                            }
                        }
                    }
                    Err(err) if err.kind() == ErrorKind::Interrupted => {}
                    Err(_) => break,
                }
            }
            let _ = gone.send(false);
        })
        .context("failed to start the parent pipe watch")?;
    Ok(Some(watch))
}

const PARENT_FD_ENV: &str = "GOBBY_PARENT_FD";

#[cfg(unix)]
fn inherited_pipe(value: &std::ffi::OsStr) -> Result<std::fs::File> {
    use std::os::fd::FromRawFd;
    let fd: std::os::fd::RawFd = value
        .to_str()
        .and_then(|v| v.parse().ok())
        .with_context(|| format!("{PARENT_FD_ENV} must be a file descriptor number"))?;
    // SAFETY: the owning runner passes exactly this descriptor to the child and
    // nothing else in gdaemon opens or closes it.
    Ok(unsafe { std::fs::File::from_raw_fd(fd) })
}

#[cfg(windows)]
fn inherited_pipe(value: &std::ffi::OsStr) -> Result<std::fs::File> {
    use std::os::windows::io::{FromRawHandle, RawHandle};
    let handle: usize = value
        .to_str()
        .and_then(|v| v.parse().ok())
        .with_context(|| format!("{PARENT_FD_ENV} must be a handle value"))?;
    // SAFETY: the owning runner makes exactly this handle inheritable for the
    // child and nothing else in gdaemon opens or closes it.
    Ok(unsafe { std::fs::File::from_raw_handle(handle as RawHandle) })
}

/// Read bootstrap (a missing file means defaults, as in Python) and refuse to serve
/// when the front door is disabled.
pub fn load_enabled_bootstrap(path: &Path) -> Result<HubDatabaseBootstrap> {
    let bootstrap =
        read_hub_database_bootstrap_file(path)?.unwrap_or_else(|| HubDatabaseBootstrap {
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
    let listener = TcpListener::bind(bind_addr(host, port).await?)
        .await
        .with_context(|| format!("failed to bind {host}:{port}"))?;
    let bound = listener
        .local_addr()
        .context("bound listener has no address")?;
    let mut listeners = vec![PublicListener { listener, backend }];
    if let Some(companion) = companion_addr(bound) {
        let listener = TcpListener::bind(companion)
            .await
            .with_context(|| format!("failed to bind the loopback companion {companion}"))?;
        listeners.push(PublicListener { listener, backend });
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
    // Stage 1 has no timer-driven Rust jobs yet. Strangler ports register here.
    let mut heartbeat = crate::heartbeat::HeartbeatHost::new();
    heartbeat.start()?;
    let acceptor = tls.map(TlsAcceptor::from);
    let mut accept_loops = JoinSet::new();
    for PublicListener { listener, backend } in listeners {
        let state = FrontDoorState::new(backend, BackendState::Down, auth.clone());
        let table = RouteTable::new(FAMILIES, routes, &state);
        accept_loops.spawn(accept(
            listener,
            FrontDoor::new(state, table),
            acceptor.clone(),
        ));
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
    listener: TcpListener,
    front_door: FrontDoor,
    acceptor: Option<TlsAcceptor>,
) -> Result<()> {
    loop {
        let (stream, peer) = listener.accept().await.context("accept failed")?;
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
}

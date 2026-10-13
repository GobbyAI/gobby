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
#[cfg(unix)]
use crate::front_door::routes::unimplemented_families;
use crate::front_door::routes::{FAMILIES, RouteTable};
#[cfg(unix)]
use crate::front_door::tls;
use crate::front_door::{FrontDoor, FrontDoorState};
use crate::nodes::NativeRoutes;

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
#[cfg(unix)]
pub fn run() -> Result<()> {
    use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
    use rand::RngCore;
    let path = bootstrap_path().context("cannot resolve the Gobby home directory")?;
    let bootstrap = load_enabled_bootstrap(&path)?;
    let native = if bootstrap.mode == RunMode::Node {
        None
    } else {
        let mut secret = [0u8; 32];
        rand::thread_rng().fill_bytes(&mut secret);
        let secret = URL_SAFE_NO_PAD.encode(secret);
        let resolver = bootstrap
            .database_url
            .as_deref()
            .map(|url| Arc::new(PostgresKeyResolver::new(url)) as Arc<dyn KeyResolver>);
        let auth = Arc::new(AuthState::new(secret.clone(), resolver, path.clone())?);
        let ports = backend_ports(bootstrap.daemon_port, bootstrap.websocket_port)?;
        for name in unimplemented_families(FAMILIES, &bootstrap.front_door.routes) {
            eprintln!(
                "gdaemon serve: front_door.routes.{name} requests a native backend gdaemon does not implement; proxying"
            );
        }
        Some((secret, auth, ports))
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
        let Some((secret, auth, (backend_http, backend_ws))) = native else {
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
            crate::state::RuntimeState::prepare(&bootstrap, home, &secret)?
        else {
            eprintln!(
                "gdaemon serve: runtime singleton claim unavailable \
                 (another owner or live service reservation): {}",
                home.join("gobby.pid").display()
            );
            return Ok(());
        };
        use gobby_core::postgres_pool::{Pool, PoolSettings};
        let pool = Pool::build(
            bootstrap
                .database_url
                .as_deref()
                .context("backend requires database_url")?,
            PoolSettings {
                max_size: 4,
                application_name: "gobby-gdaemon-nodes".into(),
                acquire_timeout: Duration::from_secs(2),
            },
        )?;
        let native_routes = if bootstrap.mode == RunMode::Hub {
            NativeRoutes::Hub(Arc::new(crate::nodes::HubNodes {
                db: pool,
                registry: Default::default(),
                timing: Default::default(),
            }))
        } else {
            NativeRoutes::Standalone(Some(Arc::new(pool)))
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
    home: std::path::PathBuf,
) -> Result<()> {
    state.run(supervisor, shutdown, bootstrap, home).await
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
}

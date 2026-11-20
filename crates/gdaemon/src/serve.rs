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
#[cfg(unix)]
use gobby_core::family::Extensions;
use gobby_core::family::FamilyContext;
use gobby_core::route_family::RouteFamily;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_util::rt::TokioIo;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::{TcpListener, TcpStream};
use tokio::task::JoinSet;
use tokio_rustls::TlsAcceptor;
use tokio_rustls::rustls::ServerConfig;

#[cfg(unix)]
use crate::family_start::{ActiveConfigSource, Disposition, disposition};
use crate::front_door::auth::AuthState;
#[cfg(unix)]
use crate::front_door::auth::{KeyResolver, PostgresKeyResolver};
use crate::front_door::compare::CompareLog;
use crate::front_door::health::BackendState;
#[cfg(unix)]
use crate::front_door::registry::selectable;
use crate::front_door::routes::{FAMILIES, RouteTable};
#[cfg(unix)]
use crate::front_door::routes::{terminal_route_latch, unimplemented_families};
#[cfg(unix)]
use crate::front_door::tls;
use crate::front_door::{FrontDoor, FrontDoorState};
#[cfg(unix)]
use crate::lifecycle::backend::BackendConfig;
use crate::nodes::NativeRoutes;
#[cfg(unix)]
use crate::websocket::broker::BroadcastBrokerPublisher;
use crate::websocket::transport::NativeWs;

/// How long an accepted connection may take to send its first byte and, for
/// TLS, finish the handshake before it is dropped.
const PREAUTH_DEADLINE: Duration = Duration::from_secs(10);

/// The routed target of the serve path's records.
const TARGET: &str = "gobby.daemon";

/// How often the configuration poller reads the active configuration.
#[cfg(unix)]
const CONFIG_POLL: Duration = Duration::from_secs(5);

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

/// A bound public listener, the loopback backend it proxies to, and whether it
/// is the WebSocket port, whose upgrades are the direct entry.
pub struct PublicListener {
    pub listener: TcpListener,
    pub backend: SocketAddr,
    pub ws_listener: bool,
}

/// Entry point for `gdaemon serve`. Builds a tokio runtime for this subcommand only.
#[cfg(unix)]
pub fn run() -> Result<()> {
    use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
    use rand::RngCore;
    let path = bootstrap_path().context("cannot resolve the Gobby home directory")?;
    let bootstrap = load_enabled_bootstrap(&path)?;
    match raise_nofile_limit(NOFILE_TARGET) {
        Ok((before, after)) => {
            tracing::warn!(target: TARGET, "gdaemon serve: fd soft limit {before} -> {after}");
        }
        Err(error) => tracing::warn!(
            target: TARGET,
            "gdaemon serve: warning: could not raise the fd soft limit: {error}"
        ),
    }
    let native = if bootstrap.mode == RunMode::Node {
        None
    } else {
        let mut secret = [0u8; 32];
        rand::thread_rng().fill_bytes(&mut secret);
        let secret = URL_SAFE_NO_PAD.encode(secret);
        // One pool serves key resolution, the lease, the hub node routes and the families.
        let pool = bootstrap
            .database_url
            .as_deref()
            .map(|url| crate::native_pools::build_serve(url).map(Arc::new))
            .transpose()?;
        let resolver = pool
            .clone()
            .map(|pool| Arc::new(PostgresKeyResolver::new(pool)) as Arc<dyn KeyResolver>);
        let auth = Arc::new(AuthState::new(secret.clone(), resolver, path.clone())?);
        let ports = backend_ports(bootstrap.daemon_port, bootstrap.websocket_port)?;
        let home = path.parent().context("bootstrap has no parent directory")?;
        // The one family list of the process: routes, activation and start all read it.
        let families: &'static [&'static dyn RouteFamily<FrontDoorState>] =
            Box::leak(selectable(pool.as_ref(), home).into_boxed_slice());
        for name in unimplemented_families(families, &bootstrap.front_door.routes) {
            tracing::warn!(
                target: TARGET,
                "gdaemon serve: front_door.routes.{name} requests a native backend gdaemon does not implement; proxying"
            );
        }
        let terminals = terminal_route_latch(&bootstrap.front_door.routes)?;
        Some((secret, auth, ports, pool, terminals, families))
    };
    let tls = tls::load(&bootstrap.front_door.tls, &bootstrap.bind_host)?;
    if let Some(loaded) = &tls {
        for name in &loaded.missing_sans {
            tracing::warn!(
                target: TARGET,
                "gdaemon serve: front_door.tls.sans entry {name} is missing from the existing certificate; \
                 remove both certificate files and restart to regenerate (nodes must log in again)"
            );
        }
        crate::logging::raw_stderr(&format!("front door certificate {}", loaded.fingerprint));
    }

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .context("failed to start the tokio runtime")?;
    runtime.block_on(async {
        let home = path.parent().context("bootstrap has no parent directory")?;
        let Some((secret, auth, (backend_http, _), pool, terminals, families)) = native else {
            let Some((node, claim)) = crate::state::NodeServices::prepare(&bootstrap, &path, home)?
            else {
                return Ok(());
            };
            // A node has no configuration epoch and no backend worker: compiled defaults,
            // and gdaemon rotates its own files.
            crate::logging::activate(crate::logging::LogSettings::for_home(home), None)?;
            let terminal_host = match gobby_core::machine::read_machine_id_from_home(home) {
                Ok(machine_id) => {
                    use crate::nodes::agents::host::{NodeEpochAuthority, node_host_options};
                    let host = gobby_terminals::host::HostSupervisor::new(
                        node_host_options(home, machine_id),
                        Arc::new(NodeEpochAuthority { agents: node.agents.clone() }),
                    );
                    if let Err(error) = host.start().await {
                        tracing::warn!(
                            target: TARGET,
                            "gdaemon node terminal host unavailable (supervisor will retry): {error}"
                        );
                    }
                    Some(host)
                }
                Err(error) => {
                    tracing::warn!(
                        target: TARGET,
                        "gdaemon node terminal host identity unavailable: {error}"
                    );
                    None
                }
            };
            crate::nodes::hooks::drain::boot_release(&node.hook_ledger).await?;
            crate::nodes::agents::workspace::retry_removals(&node.agents).await?;
            let mut heartbeat = crate::heartbeat::HeartbeatHost::new();
            crate::nodes::hooks::drain::register_jobs(&mut heartbeat, node.clone())?;
            crate::nodes::agents::reports::register_jobs(&mut heartbeat, node.clone())?;
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
            let reporter = tokio::spawn(crate::nodes::agents::reports::report_loop(node.clone()));
            let agents = node.agents.clone();
            let exit_watch = tokio::spawn(async move {
                crate::nodes::agents::launch::watch_exits(&agents).await
            });
            let result = serve_doors(
                doors,
                tls.map(|loaded| loaded.config),
                heartbeat,
                shutdown_signal(),
            )
            .await;
            channel.abort();
            let _ = channel.await;
            reporter.abort();
            let _ = reporter.await;
            exit_watch.abort();
            let _ = exit_watch.await;
            // Stop supervision only; live terminal processes survive a daemon restart.
            drop(terminal_host);
            drop(claim);
            return result;
        };
        let Some((state, supervisor)) =
            crate::state::RuntimeState::prepare(&bootstrap, home, &secret, pool.clone())?
        else {
            tracing::warn!(
                target: TARGET,
                "gdaemon serve: runtime singleton claim unavailable \
                 (another owner or live service reservation): {}",
                home.join("gobby.pid").display()
            );
            return Ok(());
        };
        let pool = pool.context("backend requires database_url")?;
        let broker = crate::websocket::broker::BroadcastBroker::default();
        let started = crate::family_start::prepare(crate::family_start::PrepareInputs {
            serve_pool: Some(Arc::clone(&pool)),
            home: home.to_owned(),
            routes: bootstrap.front_door.routes.clone(),
            publisher: Arc::new(BroadcastBrokerPublisher::new(broker.clone())),
            backend: Some(state.backend.clone()),
            lease: state.lease_state.clone(),
            epoch_source: Arc::new(ActiveConfigSource {
                pool: Arc::clone(&pool),
                token: gobby_core::grant::deployment_token(home),
            }),
            cpu_count: std::thread::available_parallelism()
                .map_or(1, |count| u32::try_from(count.get()).unwrap_or(u32::MAX)),
            families: families.to_vec(),
            extensions: Extensions::new(),
            startup: None,
        })
        .await;
        let started = match started {
            Ok(started) => Ok(started),
            Err(failure) => match disposition(&failure) {
                Disposition::Exit => anyhow::bail!(
                    "family {} failed to start: {}",
                    failure.family,
                    failure.message
                ),
                Disposition::Refuse { reason } => {
                    tracing::warn!(
                        target: TARGET,
                        "gdaemon serve: family {} refused the start: {}",
                        failure.family,
                        failure.message
                    );
                    Err(reason)
                }
            },
        };
        let (applier, mut first_read) = match &started {
            Ok(started) => {
                // The backend worker's handler rotates the files it shares with gdaemon.
                let command = BackendConfig::from_environment(home)?.command;
                let command: Vec<_> = command.iter().map(|part| part.to_string_lossy()).collect();
                let routed = RoutedActivation {
                    settings: crate::logging::LogSettings::for_home(home),
                    backend_command: command.join(" "),
                    start_warnings: started.start_warnings.clone(),
                    config: started.ctx.subscribe_config(),
                };
                routed.start(started.config_read).await?
            }
            Err(_) => (None, None),
        };
        let native_routes = if bootstrap.mode == RunMode::Hub {
            NativeRoutes::Hub(Arc::new(crate::nodes::HubNodes {
                db: Arc::clone(&pool),
                registry: Default::default(),
                timing: Default::default(),
            }))
        } else {
            NativeRoutes::Standalone(Some(Arc::clone(&pool)))
        };
        let compare_log = Arc::new(CompareLog::open(home).with_context(|| {
            format!(
                "cannot open the front-door compare log under {}",
                home.display()
            )
        })?);
        let mut listeners = bind(
            &bootstrap.bind_host,
            bootstrap.daemon_port,
            backend_http,
            false,
        )
        .await?;
        listeners.extend(
            bind(
                &bootstrap.bind_host,
                bootstrap.websocket_port,
                backend_http,
                true,
            )
            .await?,
        );
        // The configuration poller runs once the doors are bound.
        let poller = started.as_ref().ok().map(|started| {
            crate::family_start::spawn_config_poller(
                Arc::new(ActiveConfigSource {
                    pool: Arc::clone(&pool),
                    token: gobby_core::grant::deployment_token(home),
                }),
                started.ctx.config_publisher(),
                CONFIG_POLL,
                first_read.take(),
            )
        });
        let native_ws = NativeWs::new(
            broker,
            secret.clone(),
            Some(state.write_token.clone()),
            SocketAddr::from(([127, 0, 0, 1], backend_http)),
        );
        let retire = tokio::spawn({
            let native_ws = native_ws.clone();
            let backend = state.backend.clone();
            async move { native_ws.retire_on_backend_change(backend).await }
        });
        let (stop, shutdown) = tokio::sync::watch::channel(false);
        let (context, mut lifecycle) = match started {
            Ok(started) => (
                Some(Arc::new(started.ctx.clone())),
                tokio::spawn(owner_run(
                    state.clone(),
                    supervisor,
                    shutdown,
                    bootstrap.clone(),
                    terminals,
                    home.to_owned(),
                    started,
                )),
            ),
            Err(reason) => (
                None,
                tokio::spawn(crate::state::RuntimeState::hold_refused(
                    supervisor, reason, shutdown,
                )),
            ),
        };
        let mut owner_result = None;
        let result = serve_with_runtime(
            listeners,
            &bootstrap.front_door.routes,
            tls.map(|loaded| loaded.config),
            auth,
            ListenerShared {
                runtime: Some(state),
                native: native_routes,
                compare_log: Some(compare_log),
                families,
                native_ws: Some(native_ws),
                context,
            },
            async {
                tokio::select! {
                    () = shutdown_signal() => {},
                    finished = &mut lifecycle => { owner_result = Some(finished); },
                }
            },
        )
        .await;
        retire.abort();
        // The applier first: a gate still waiting must not see the poller's signal dropped.
        for task in applier.iter().chain(&poller) {
            task.abort();
        }
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
    families: crate::family_start::ProcessFamilies,
) -> Result<()> {
    state
        .run(supervisor, shutdown, bootstrap, terminals, home, families)
        .await
}

/// Routed logging for a hub or standalone start. It activates only after the first
/// successful configuration read; until then the pre-configuration sink (stderr) stays.
#[cfg(unix)]
struct RoutedActivation {
    settings: crate::logging::LogSettings,
    backend_command: String,
    start_warnings: Vec<String>,
    config: tokio::sync::watch::Receiver<gobby_core::family::ConfigEpoch>,
}

/// The epoch applier task, and the signal the poller fires on its first successful read
/// when activation waits for it.
#[cfg(unix)]
type RoutedTasks = (
    Option<tokio::task::JoinHandle<()>>,
    Option<tokio::sync::oneshot::Sender<()>>,
);

#[cfg(unix)]
impl RoutedActivation {
    /// When the start's own read succeeded, activates now, so files that cannot be opened
    /// fail the start. Otherwise a task waits for the poller's first successful read and
    /// activates then; a failure there is reported on stderr and the pre-configuration
    /// sink stays. Each path activates, applies the current epoch, replays the start's
    /// warnings, then follows the epochs.
    async fn start(self, config_read: bool) -> Result<RoutedTasks> {
        let Self {
            settings,
            backend_command,
            start_warnings,
            mut config,
        } = self;
        // The files open under the epoch current at activation; the applier then applies
        // that epoch from the same channel and reports its rejected settings.
        let activate = move |current: gobby_core::family::ConfigEpoch| {
            crate::logging::activate_at(settings, Some(&backend_command), &current.active_values)
        };
        if config_read {
            crate::family_start::activate_after_first_read(None, config.clone(), activate).await?;
            let applier = crate::logging::epoch::spawn_applier(config);
            crate::family_start::replay_start_warnings(&start_warnings);
            return Ok((Some(applier), None));
        }
        let (signal, readable) = tokio::sync::oneshot::channel();
        let applier = tokio::spawn(async move {
            let gate = config.clone();
            match crate::family_start::activate_after_first_read(Some(readable), gate, activate)
                .await
            {
                Ok(()) => {
                    crate::logging::epoch::apply_current(&mut config);
                    crate::family_start::replay_start_warnings(&start_warnings);
                    crate::logging::epoch::follow(config).await;
                }
                Err(error) => crate::logging::raw_stderr(&format!(
                    "gdaemon serve: routed logging did not activate; records stay on stderr: \
                     {error:#}"
                )),
            }
        });
        Ok((Some(applier), Some(signal)))
    }
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
pub async fn bind(
    host: &str,
    port: u16,
    backend_port: u16,
    ws_listener: bool,
) -> Result<Vec<PublicListener>> {
    let backend = SocketAddr::from(([127, 0, 0, 1], backend_port));
    Ok(bind_listeners(host, port)
        .await?
        .into_iter()
        .map(|listener| PublicListener {
            listener,
            backend,
            ws_listener,
        })
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
    let shared = ListenerShared {
        families: FAMILIES,
        ..ListenerShared::default()
    };
    serve_with_runtime(listeners, routes, tls, auth, shared, shutdown).await
}

/// What `run` adds to every listener's [`FrontDoorState`]; `serve` sets only `families`.
#[derive(Default)]
struct ListenerShared {
    #[cfg(unix)]
    runtime: Option<Arc<crate::state::RuntimeState>>,
    native: NativeRoutes,
    compare_log: Option<Arc<CompareLog>>,
    families: &'static [&'static dyn RouteFamily<FrontDoorState>],
    /// One transport shared by every listener; drained after the doors stop.
    native_ws: Option<NativeWs>,
    /// The process's family context; without one each door keeps the unconfigured one.
    context: Option<Arc<FamilyContext>>,
}

async fn serve_with_runtime(
    listeners: Vec<PublicListener>,
    routes: &BTreeMap<String, RouteBackend>,
    tls: Option<Arc<ServerConfig>>,
    auth: Arc<AuthState>,
    shared: ListenerShared,
    shutdown: impl Future<Output = ()>,
) -> Result<()> {
    let mut doors = Vec::with_capacity(listeners.len());
    for PublicListener {
        listener,
        backend,
        ws_listener,
    } in listeners
    {
        let mut state = FrontDoorState {
            #[cfg(unix)]
            runtime: shared.runtime.clone(),
            native: shared.native.clone(),
            compare_log: shared.compare_log.clone(),
            ws_listener,
            ..FrontDoorState::new(backend, BackendState::Down, auth.clone())
        };
        if let Some(native_ws) = &shared.native_ws {
            state.native_ws = native_ws.clone();
        }
        if let Some(context) = &shared.context {
            state.context = Arc::clone(context);
        }
        let table = RouteTable::new(shared.families, routes, &state);
        doors.push((listener, FrontDoor::new(state, table)));
    }
    let result = serve_doors(doors, tls, crate::heartbeat::HeartbeatHost::new(), shutdown).await;
    if let Some(native_ws) = &shared.native_ws {
        native_ws.shutdown().await;
    }
    result
}

/// Serve prepared backend or node doors on their public listeners.
pub async fn serve_doors(
    doors: Vec<(TcpListener, FrontDoor)>,
    tls: Option<Arc<ServerConfig>>,
    mut heartbeat: crate::heartbeat::HeartbeatHost,
    shutdown: impl Future<Output = ()>,
) -> Result<()> {
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
                    tracing::warn!(
                        target: TARGET,
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
mod tests;

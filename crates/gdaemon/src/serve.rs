//! `gdaemon serve`: bind the public HTTP and WS ports and run the front door.

use std::collections::BTreeMap;
use std::convert::Infallible;
use std::future::Future;
use std::net::SocketAddr;
use std::path::Path;

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
use tokio::net::TcpListener;
use tokio::task::JoinSet;

use crate::front_door::health::BackendState;
use crate::front_door::routes::{FAMILIES, RouteTable, unimplemented_families};
use crate::front_door::{FrontDoor, FrontDoorState};

/// A bound public listener and the loopback backend it proxies to.
pub struct PublicListener {
    pub listener: TcpListener,
    pub backend: SocketAddr,
}

/// Entry point for `gdaemon serve`. Builds a tokio runtime for this subcommand only.
pub fn run() -> Result<()> {
    let path = bootstrap_path().context("cannot resolve the Gobby home directory")?;
    let bootstrap = load_enabled_bootstrap(&path)?;
    let (backend_http, backend_ws) =
        backend_ports(bootstrap.daemon_port, bootstrap.websocket_port)?;
    for name in unimplemented_families(FAMILIES, &bootstrap.front_door.routes) {
        eprintln!(
            "gdaemon serve: front_door.routes.{name} requests a native backend gdaemon does not implement; proxying"
        );
    }

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .context("failed to start the tokio runtime")?;
    runtime.block_on(async {
        let host = bootstrap.bind_host.as_str();
        let listeners = vec![
            bind(host, bootstrap.daemon_port, backend_http).await?,
            bind(host, bootstrap.websocket_port, backend_ws).await?,
        ];
        let parent_gone = watch_parent_fd()?;
        let shutdown = async move {
            match parent_gone {
                Some(gone) => tokio::select! {
                    () = shutdown_signal() => {}
                    _ = gone => {}
                },
                None => shutdown_signal().await,
            }
        };
        serve(listeners, &bootstrap.front_door.routes, shutdown).await
    })
}

/// The runner that owns this `serve` passes the read end of a liveness pipe in
/// `GOBBY_PARENT_FD`; the receiver resolves on EOF, meaning the runner is gone by
/// any cause, SIGKILL included. Without the variable there is no watch. The read
/// runs on a detached thread, not the blocking pool, so a runtime drop never
/// waits on it.
fn watch_parent_fd() -> Result<Option<tokio::sync::oneshot::Receiver<()>>> {
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
            loop {
                match pipe.read(&mut buf) {
                    Ok(0) => break,
                    Ok(_) => {}
                    Err(err) if err.kind() == ErrorKind::Interrupted => {}
                    Err(_) => break,
                }
            }
            let _ = gone.send(());
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

async fn bind(host: &str, port: u16, backend_port: u16) -> Result<PublicListener> {
    let listener = TcpListener::bind(bind_addr(host, port).await?)
        .await
        .with_context(|| format!("failed to bind {host}:{port}"))?;
    Ok(PublicListener {
        listener,
        backend: SocketAddr::from(([127, 0, 0, 1], backend_port)),
    })
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

/// Serve every listener until `shutdown` resolves.
pub async fn serve(
    listeners: Vec<PublicListener>,
    routes: &BTreeMap<String, RouteBackend>,
    shutdown: impl Future<Output = ()>,
) -> Result<()> {
    let mut accept_loops = JoinSet::new();
    for PublicListener { listener, backend } in listeners {
        let state = FrontDoorState::new(backend, BackendState::Down);
        let table = RouteTable::new(FAMILIES, routes, &state);
        accept_loops.spawn(accept(listener, FrontDoor::new(state, table)));
    }
    tokio::select! {
        () = shutdown => Ok(()),
        Some(finished) = accept_loops.join_next() => {
            finished.context("front door accept loop panicked")?
        }
    }
}

async fn accept(listener: TcpListener, front_door: FrontDoor) -> Result<()> {
    loop {
        let (stream, _) = listener.accept().await.context("accept failed")?;
        let front_door = front_door.clone();
        tokio::spawn(async move {
            let service = service_fn(move |request: hyper::Request<hyper::body::Incoming>| {
                let front_door = front_door.clone();
                async move { Ok::<_, Infallible>(front_door.handle(request.map(Body::new)).await) }
            });
            let _ = http1::Builder::new()
                .serve_connection(TokioIo::new(stream), service)
                .with_upgrades()
                .await;
        });
    }
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

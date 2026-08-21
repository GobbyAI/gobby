//! gterm host process: sockets, control protocol, and supervised shutdown.

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tokio::net::UnixListener;
use tracing::info;

use crate::ipc::{prepare_socket_path, restrict_socket_permissions};

mod control;

const CONTROL_SOCKET: &str = "gterm-control.sock";
const FRAMES_SOCKET: &str = "gterm-frames.sock";
const PID_FILE: &str = "gterm.pid";
const TOKEN_FILE: &str = "gterm-control.token";

pub async fn run() -> io::Result<()> {
    let args = HostArgs::parse();
    init_tracing(&args.log_file);

    let token = fs::read_to_string(&args.token_file)
        .map_err(|err| io::Error::new(err.kind(), format!("control token: {err}")))?
        .trim()
        .to_string();
    if token.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "control token file is empty",
        ));
    }

    let host_epoch = uuid::Uuid::new_v4().to_string();
    let version = env!("CARGO_PKG_VERSION").to_string();
    let host_pid = std::process::id();
    write_pidfile(&args.pid_file, host_pid)?;

    let control_path = args.socket_dir.join(CONTROL_SOCKET);
    let frames_path = args.socket_dir.join(FRAMES_SOCKET);
    prepare_socket_path(&control_path, |path| {
        format!("gterm control socket busy at {}", path.display())
    })?;
    prepare_socket_path(&frames_path, |path| {
        format!("gterm frames socket busy at {}", path.display())
    })?;

    let control_listener = UnixListener::bind(&control_path)?;
    restrict_socket_permissions(&control_path, 0o600)?;
    let frames_listener = UnixListener::bind(&frames_path)?;
    restrict_socket_permissions(&frames_path, 0o600)?;

    let draining = Arc::new(AtomicBool::new(false));
    let (shutdown_tx, mut shutdown_rx) = tokio::sync::watch::channel(false);
    let token = Arc::new(token);
    let host_epoch = Arc::new(host_epoch);
    let version = Arc::new(version);
    let shutdown_grace_ms = args.shutdown_grace_ms;

    info!(
        epoch = %host_epoch,
        pid = host_pid,
        "gterm host listening"
    );

    let frames_task = tokio::spawn(async move {
        loop {
            match frames_listener.accept().await {
                Ok((_stream, _)) => {}
                Err(_) => break,
            }
        }
    });

    let control_accept = {
        let draining = Arc::clone(&draining);
        let shutdown_tx = shutdown_tx.clone();
        let token = Arc::clone(&token);
        let host_epoch = Arc::clone(&host_epoch);
        let version = Arc::clone(&version);
        tokio::spawn(async move {
            loop {
                match control_listener.accept().await {
                    Ok((stream, _)) => {
                        let draining = Arc::clone(&draining);
                        let shutdown_tx = shutdown_tx.clone();
                        let token = Arc::clone(&token);
                        let host_epoch = Arc::clone(&host_epoch);
                        let version = Arc::clone(&version);
                        tokio::spawn(async move {
                            control::handle_connection(
                                stream,
                                token,
                                host_epoch,
                                version,
                                host_pid,
                                draining,
                                shutdown_tx,
                                shutdown_grace_ms,
                            )
                            .await;
                        });
                    }
                    Err(_) => break,
                }
            }
        })
    };

    let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tokio::select! {
        _ = shutdown_rx.changed() => {}
        _ = sigterm.recv() => {
            draining.store(true, Ordering::SeqCst);
        }
    }

    control_accept.abort();
    frames_task.abort();
    let _ = fs::remove_file(&control_path);
    let _ = fs::remove_file(&frames_path);
    let _ = fs::remove_file(&args.pid_file);
    tokio::time::sleep(Duration::from_millis(150)).await;
    Ok(())
}

#[derive(Debug)]
struct HostArgs {
    socket_dir: PathBuf,
    token_file: PathBuf,
    pid_file: PathBuf,
    log_file: PathBuf,
    shutdown_grace_ms: u64,
}

impl HostArgs {
    fn parse() -> Self {
        let mut socket_dir = std::env::var("GTERM_SOCKET_DIR")
            .ok()
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                dirs_home()
                    .map(|home| home.join(".gobby"))
                    .unwrap_or_else(|| PathBuf::from("."))
            });
        let mut args = std::env::args().skip(1);
        let _cmd = args.next();
        while let Some(arg) = args.next() {
            if arg == "--socket-dir" {
                if let Some(value) = args.next() {
                    socket_dir = PathBuf::from(value);
                }
            }
        }
        let log_file = std::env::var("GTERM_LOG_FILE")
            .ok()
            .map(PathBuf::from)
            .unwrap_or_else(|| socket_dir.join("logs").join("gterm.log"));
        Self {
            token_file: socket_dir.join(TOKEN_FILE),
            pid_file: socket_dir.join(PID_FILE),
            log_file,
            socket_dir,
            shutdown_grace_ms: 10_000,
        }
    }
}

fn dirs_home() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from)
}

fn write_pidfile(path: &Path, pid: u32) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut file = fs::File::create(path)?;
    writeln!(file, "{pid}")?;
    Ok(())
}

fn init_tracing(log_file: &Path) {
    if let Some(parent) = log_file.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_file)
        .ok();
    let env_filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));
    if let Some(file) = file {
        let _ = tracing_subscriber::fmt()
            .with_env_filter(env_filter)
            .with_writer(std::sync::Mutex::new(file))
            .try_init();
    } else {
        let _ = tracing_subscriber::fmt()
            .with_env_filter(env_filter)
            .try_init();
    }
}

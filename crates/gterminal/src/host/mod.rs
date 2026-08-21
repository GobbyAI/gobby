//! gterm host process: sockets, control protocol, and supervised shutdown.

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use tokio::net::UnixListener;
use tracing::info;

use crate::ipc::{prepare_socket_path, restrict_socket_permissions};

mod config;
mod control;
mod embed;
mod frames;
mod helpers;
mod ledger;
pub mod poll;
#[cfg(all(unix, feature = "vt-engine"))]
mod spawn;
mod state;

pub use poll::{
    classify_poll, parse_poll_batch, truncate_attach_history, PollClass, POLL_FIELD_COUNT,
};

use config::HostConfig;
use state::HostState;

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

    let host_config = args
        .host_config
        .validate()
        .map_err(|err| io::Error::new(err.kind(), format!("gterm host config: {err}")))?;
    let local_token = read_local_token(&args.socket_dir);
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

    let (shutdown_tx, mut shutdown_rx) = tokio::sync::watch::channel(false);
    let state = HostState::new(
        host_config,
        token,
        local_token,
        host_epoch.clone(),
        version,
        host_pid,
        shutdown_tx.clone(),
    );

    info!(
        epoch = %host_epoch,
        pid = host_pid,
        "gterm host listening"
    );

    let frames_task = {
        let state = Arc::clone(&state);
        tokio::spawn(async move {
            loop {
                match frames_listener.accept().await {
                    Ok((stream, _)) => {
                        let state = Arc::clone(&state);
                        tokio::spawn(async move {
                            frames::handle_connection(stream, state).await;
                        });
                    }
                    Err(_) => break,
                }
            }
        })
    };

    let control_accept = {
        let state = Arc::clone(&state);
        tokio::spawn(async move {
            loop {
                match control_listener.accept().await {
                    Ok((stream, _)) => {
                        let state = Arc::clone(&state);
                        tokio::spawn(async move {
                            control::handle_connection(stream, state).await;
                        });
                    }
                    Err(_) => break,
                }
            }
        })
    };

    let ticker = {
        let state = Arc::clone(&state);
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_millis(30));
            loop {
                interval.tick().await;
                state.expire_prepared().await;
                state.broadcast_frames().await;
            }
        })
    };

    let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tokio::select! {
        _ = shutdown_rx.changed() => {}
        _ = sigterm.recv() => {
            state.draining.store(true, Ordering::SeqCst);
        }
    }
    ticker.abort();

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
    host_config: HostConfig,
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
        let mut host_config = HostConfig::default();
        let mut args = std::env::args().skip(1);
        let _cmd = args.next();
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--socket-dir" => {
                    if let Some(value) = args.next() {
                        socket_dir = PathBuf::from(value);
                    }
                }
                "--max-attachments-per-terminal" => {
                    host_config.max_attachments_per_terminal = parse_u32(args.next());
                }
                "--max-attachments-total" => {
                    host_config.max_attachments_total = parse_u32(args.next());
                }
                "--max-attached-terminals" => {
                    host_config.max_attached_terminals = parse_u32(args.next());
                }
                "--native-scrollback-max-lines" => {
                    host_config.native_scrollback_max_lines = parse_u32(args.next());
                }
                "--native-scrollback-max-bytes" => {
                    host_config.native_scrollback_max_bytes = parse_u32(args.next());
                }
                "--tmux-attach-history-lines" => {
                    host_config.tmux_attach_history_lines = parse_u32(args.next());
                }
                "--tmux-attach-history-max-bytes" => {
                    host_config.tmux_attach_history_max_bytes = parse_u32(args.next());
                }
                "--tmux-poll-interval-ms" => {
                    host_config.tmux_poll_interval_ms = parse_u32(args.next());
                }
                "--tmux-poll-backoff-ceiling-ms" => {
                    host_config.tmux_poll_backoff_ceiling_ms = parse_u32(args.next());
                }
                _ => {}
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
            host_config,
        }
    }
}

fn parse_u32(value: Option<String>) -> u32 {
    value.and_then(|v| v.parse().ok()).unwrap_or(u32::MAX)
}

fn read_local_token(socket_dir: &Path) -> String {
    let candidates = [
        socket_dir.join("local_cli_token"),
        dirs_home()
            .map(|home| home.join(".gobby").join("local_cli_token"))
            .unwrap_or_else(|| PathBuf::from("local_cli_token")),
    ];
    for path in candidates {
        if let Ok(text) = fs::read_to_string(&path) {
            let trimmed = text.trim();
            if !trimmed.is_empty() {
                return trimmed.to_string();
            }
        }
    }
    String::new()
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

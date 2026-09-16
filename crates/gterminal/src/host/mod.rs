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

mod backpressure;
mod config;
mod control;
mod embed;
mod events;
mod frames;
#[cfg(all(unix, feature = "vt-engine"))]
pub(crate) mod gate;
mod helpers;
mod ledger;
mod native_ops;
pub mod poll;
#[cfg(all(unix, feature = "vt-engine"))]
mod spawn;
mod state;
mod write;

pub use backpressure::{FrameMailbox, PushResult};
pub use poll::{
    classify_poll, parse_poll_batch, truncate_attach_history, PollClass, POLL_FIELD_COUNT,
};

use config::HostConfig;
use state::HostState;

const CONTROL_SOCKET: &str = "gterm-control.sock";
const FRAMES_SOCKET: &str = "gterm-frames.sock";
const PID_FILE: &str = "gterm.pid";
const TOKEN_FILE: &str = "gterm-control.token";
const LOCAL_CLI_TOKEN_FILE: &str = "local_cli_token";

pub async fn run() -> io::Result<()> {
    let args = HostArgs::parse();
    init_tracing(&args.log_file);
    crate::platform::watch_terminal_resize_signal();

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
            while let Ok((stream, _)) = frames_listener.accept().await {
                let state = Arc::clone(&state);
                tokio::spawn(async move {
                    frames::handle_connection(stream, state).await;
                });
            }
        })
    };

    let control_accept = {
        let state = Arc::clone(&state);
        tokio::spawn(async move {
            while let Ok((stream, _)) = control_listener.accept().await {
                let state = Arc::clone(&state);
                tokio::spawn(async move {
                    control::handle_connection(stream, state).await;
                });
            }
        })
    };

    let ticker = {
        let state = Arc::clone(&state);
        let socket_dir = args.socket_dir.clone();
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_millis(30));
            loop {
                interval.tick().await;
                match tokio::fs::metadata(&socket_dir).await {
                    Ok(metadata) if metadata.is_dir() => {}
                    Ok(_) => {
                        state.socket_dir_removed.store(true, Ordering::SeqCst);
                        let _ = state.shutdown.send(true);
                        break;
                    }
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {
                        state.socket_dir_removed.store(true, Ordering::SeqCst);
                        let _ = state.shutdown.send(true);
                        break;
                    }
                    Err(_) => continue,
                }
                let _ = crate::platform::take_terminal_resize_signal();
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

    let socket_dir_removed = state.socket_dir_removed.load(Ordering::SeqCst);
    if socket_dir_removed {
        info!(reason = "socket_dir_removed", "gterm host shutting down");
    }

    control_accept.abort();
    frames_task.abort();
    let _ = fs::remove_file(&control_path);
    let _ = fs::remove_file(&frames_path);
    let _ = fs::remove_file(&args.pid_file);
    if !socket_dir_removed {
        tokio::time::sleep(Duration::from_millis(args.shutdown_grace_ms)).await;
    }
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
    /// Parse `gterm host` argv, or print usage and exit (0 for help, 2 for a
    /// bad argument) before anything touches the socket dir (#22425).
    fn parse() -> Self {
        match Self::from_args(std::env::args().skip(2)) {
            Ok(args) => args,
            Err(ArgError::Help) => {
                print!("{HOST_USAGE}");
                std::process::exit(0);
            }
            Err(ArgError::Invalid(message)) => {
                eprintln!("gterm host: {message}");
                eprint!("{HOST_USAGE}");
                std::process::exit(2);
            }
        }
    }

    fn from_args<I: IntoIterator<Item = String>>(argv: I) -> Result<Self, ArgError> {
        let mut socket_dir = std::env::var("GTERM_SOCKET_DIR")
            .ok()
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                dirs_home()
                    .map(|home| home.join(".gobby"))
                    .unwrap_or_else(|| PathBuf::from("."))
            });
        let mut host_config = HostConfig::default();
        let mut args = argv.into_iter();
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--help" | "-h" => return Err(ArgError::Help),
                "--socket-dir" => {
                    socket_dir = PathBuf::from(value_for(&arg, &mut args)?);
                }
                "--max-attachments-per-terminal" => {
                    host_config.max_attachments_per_terminal =
                        parse_u32(value_for(&arg, &mut args)?)
                }
                "--max-attachments-total" => {
                    host_config.max_attachments_total = parse_u32(value_for(&arg, &mut args)?)
                }
                "--max-attached-terminals" => {
                    host_config.max_attached_terminals = parse_u32(value_for(&arg, &mut args)?)
                }
                "--native-scrollback-max-lines" => {
                    host_config.native_scrollback_max_lines = parse_u32(value_for(&arg, &mut args)?)
                }
                "--native-scrollback-max-bytes" => {
                    host_config.native_scrollback_max_bytes = parse_u32(value_for(&arg, &mut args)?)
                }
                "--tmux-attach-history-lines" => {
                    host_config.tmux_attach_history_lines = parse_u32(value_for(&arg, &mut args)?)
                }
                "--tmux-attach-history-max-bytes" => {
                    host_config.tmux_attach_history_max_bytes =
                        parse_u32(value_for(&arg, &mut args)?)
                }
                "--tmux-poll-interval-ms" => {
                    host_config.tmux_poll_interval_ms = parse_u32(value_for(&arg, &mut args)?)
                }
                "--tmux-poll-backoff-ceiling-ms" => {
                    host_config.tmux_poll_backoff_ceiling_ms =
                        parse_u32(value_for(&arg, &mut args)?)
                }
                "--delta-queue-bytes" => {
                    host_config.delta_queue_bytes = parse_u32(value_for(&arg, &mut args)?)
                }
                "--lag-timeout-ms" => {
                    host_config.lag_timeout_ms = parse_u32(value_for(&arg, &mut args)?)
                }
                "--control-deadline-ms" => {
                    host_config.control_deadline_ms = parse_u32(value_for(&arg, &mut args)?)
                }
                "--control-queue-entries" => {
                    host_config.control_queue_entries = parse_u32(value_for(&arg, &mut args)?)
                }
                "--event-queue-bytes" => {
                    host_config.event_queue_bytes = parse_u32(value_for(&arg, &mut args)?)
                }
                _ => return Err(ArgError::Invalid(format!("unknown argument `{arg}`"))),
            }
        }
        let log_file = std::env::var("GTERM_LOG_FILE")
            .ok()
            .map(PathBuf::from)
            .unwrap_or_else(|| socket_dir.join("logs").join("gterm.log"));
        Ok(Self {
            token_file: socket_dir.join(TOKEN_FILE),
            pid_file: socket_dir.join(PID_FILE),
            log_file,
            socket_dir,
            shutdown_grace_ms: 150,
            host_config,
        })
    }
}

/// Why `gterm host` stops before starting: the caller asked for help, or the
/// argv is unusable.
enum ArgError {
    Help,
    Invalid(String),
}

const HOST_USAGE: &str = "\
usage: gterm host [OPTIONS]

Options:
      --socket-dir PATH                  control/frames sockets, token, and pidfile directory
      --max-attachments-per-terminal N   attachment ceiling for one terminal
      --max-attachments-total N          attachment ceiling for the host
      --max-attached-terminals N         attached terminal ceiling for the host
      --native-scrollback-max-lines N    native scrollback line ceiling
      --native-scrollback-max-bytes N    native scrollback byte ceiling
      --tmux-attach-history-lines N      tmux attach history lines per observer
      --tmux-attach-history-max-bytes N  tmux attach history byte ceiling
      --tmux-poll-interval-ms N          tmux poll interval in milliseconds
      --tmux-poll-backoff-ceiling-ms N   tmux poll backoff ceiling in milliseconds
      --delta-queue-bytes N              frame delta queue byte ceiling
      --lag-timeout-ms N                 frame lag timeout in milliseconds
      --control-deadline-ms N            control delivery deadline in milliseconds
      --control-queue-entries N          control queue entry ceiling
      --event-queue-bytes N              event queue byte ceiling
  -h, --help                             print this help and exit

Environment: GTERM_SOCKET_DIR overrides the default socket dir, GTERM_LOG_FILE
the default log path.
";

fn value_for(flag: &str, args: &mut impl Iterator<Item = String>) -> Result<String, ArgError> {
    args.next()
        .ok_or_else(|| ArgError::Invalid(format!("`{flag}` needs a value")))
}

fn parse_u32(value: String) -> u32 {
    value.parse().unwrap_or(u32::MAX)
}

fn read_local_token(socket_dir: &Path) -> String {
    read_local_token_from(socket_dir, gobby_home().as_deref())
}

fn read_local_token_from(socket_dir: &Path, gobby_home: Option<&Path>) -> String {
    let mut candidates = vec![socket_dir.join(LOCAL_CLI_TOKEN_FILE)];
    if let Some(home) = gobby_home {
        candidates.push(home.join(LOCAL_CLI_TOKEN_FILE));
    }
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

/// Gobby home for this host, honouring `GOBBY_HOME` exactly as the daemon does.
///
/// An isolated daemon sets `GOBBY_HOME` and writes `local_cli_token` there. Falling
/// straight through to `~/.gobby` would make the host expect the machine-wide operator
/// token and reject the credential its own daemon sends.
fn gobby_home() -> Option<PathBuf> {
    if let Some(configured) = std::env::var_os("GOBBY_HOME") {
        let path = PathBuf::from(configured);
        if !path.as_os_str().is_empty() {
            return Some(path);
        }
    }
    dirs_home().map(|home| home.join(".gobby"))
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

#[cfg(test)]
mod tests;

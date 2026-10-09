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
#[cfg(all(unix, feature = "vt-engine"))]
pub mod handover;
mod helpers;
pub mod image;
mod ledger;
mod native_ops;
pub mod poll;
#[cfg(all(unix, feature = "vt-engine"))]
pub(crate) mod sigterm;
#[cfg(all(unix, feature = "vt-engine"))]
mod spawn;
mod state;
mod submit;
mod theme;
#[cfg(all(unix, feature = "vt-engine"))]
mod upgrade;
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

/// Commit steps a restore leaves for after the listeners accept. A build
/// without the vt engine never restores, so it has none.
#[cfg(all(unix, feature = "vt-engine"))]
type PendingRestore = handover::restore::PendingCommit;
#[cfg(not(all(unix, feature = "vt-engine")))]
type PendingRestore = std::convert::Infallible;

/// The handover state format versions this build can restore.
#[cfg(all(unix, feature = "vt-engine"))]
const RESUMABLE_FORMATS: &[u32] = handover::SUPPORTED_FORMAT_VERSIONS;
#[cfg(not(all(unix, feature = "vt-engine")))]
const RESUMABLE_FORMATS: &[u32] = &[];

pub async fn run() -> io::Result<()> {
    let args = HostArgs::parse();
    init_tracing(&args.log_file);
    crate::platform::watch_terminal_resize_signal();

    // A restore runs from the pin the earlier image exec'd and adopts its
    // sockets and pidfile as they are. From here to Commit, an error or a
    // panic ends in the fallback.
    #[cfg(all(unix, feature = "vt-engine"))]
    let restoring = args.resume.as_ref().map(|resume| {
        let (carried, fallback) = handover::fallback::begin(&resume.state, resume.fallback);
        let panic_guard = fallback.arm();
        (resume, carried, fallback, panic_guard)
    });
    #[cfg(not(all(unix, feature = "vt-engine")))]
    let restoring: Option<std::convert::Infallible> = match &args.resume {
        Some(Resume { state, fallback }) => {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                format!(
                    "cannot restore {}{}: this gterm has no vt engine",
                    state.display(),
                    if *fallback { " as a fallback" } else { "" }
                ),
            ))
        }
        None => None,
    };

    let setup = (|| {
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
        Ok((token, host_config))
    })();
    #[cfg(all(unix, feature = "vt-engine"))]
    let setup = match (setup, &restoring) {
        (Err(err), Some((_, _, fallback, _))) => fallback.fail(&err),
        (setup, _) => setup,
    };
    let (token, host_config) = setup?;
    let images_dir = args.socket_dir.join(image::IMAGES_DIR);
    let host_pid = std::process::id();
    let control_path = args.socket_dir.join(CONTROL_SOCKET);
    let frames_path = args.socket_dir.join(FRAMES_SOCKET);
    let (shutdown_tx, mut shutdown_rx) = tokio::sync::watch::channel(false);

    let (state, control_listener, frames_listener, running_image, pending) = match restoring {
        #[cfg(not(all(unix, feature = "vt-engine")))]
        Some(never) => match never {},
        #[cfg(all(unix, feature = "vt-engine"))]
        Some((resume, carried, fallback, panic_guard)) => {
            let staged = (|| {
                let exe = std::env::current_exe()?;
                let running_image = image::pinned_image(&images_dir, &exe)?.ok_or_else(|| {
                    io::Error::other(format!("{} is not a pinned gterm image", exe.display()))
                })?;
                let staged = handover::restore::stage(
                    carried,
                    &resume.state,
                    &args.pid_file,
                    host_config.native_scrollback_max_bytes as usize,
                    host_config.event_queue_bytes as usize,
                    resume.fallback,
                )?;
                io::Result::Ok((running_image, staged))
            })();
            let (running_image, staged) = staged.unwrap_or_else(|err| fallback.fail(&err));
            let state = HostState::restored(
                host_config,
                token,
                running_image.clone(),
                host_pid,
                shutdown_tx.clone(),
                staged.host,
            );
            panic_guard.disarm();
            let mut commit = staged.commit;
            commit.take_ownership(&state).await;
            (
                state,
                staged.control,
                staged.frames,
                running_image,
                Some(commit),
            )
        }
        None => {
            let running_image = run_from_pin(&images_dir)
                .map_err(|err| io::Error::new(err.kind(), format!("gterm image pin: {err}")))?;
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
            // Only a host that owns both sockets may publish its pid: a second host
            // losing the busy check above must leave the live host's pidfile alone.
            write_pidfile(&args.pid_file, host_pid)?;
            let state = HostState::new(
                host_config,
                token,
                uuid::Uuid::new_v4().to_string(),
                running_image.clone(),
                host_pid,
                shutdown_tx.clone(),
            );
            (
                state,
                control_listener,
                frames_listener,
                running_image,
                None::<PendingRestore>,
            )
        }
    };

    // SIGTERM stays blocked until both handlers are in place, so one that
    // reached the earlier image during an upgrade drains this one.
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    #[cfg(all(unix, feature = "vt-engine"))]
    sigterm::start(&state.draining)?;
    #[cfg(all(unix, feature = "vt-engine"))]
    state.attempts.install(upgrade::UpgradeContext::new(
        args.socket_dir.clone(),
        std::os::fd::AsRawFd::as_raw_fd(&control_listener),
        std::os::fd::AsRawFd::as_raw_fd(&frames_listener),
    ));

    info!(
        epoch = %state.host_epoch,
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
    if let Some(commit) = pending {
        #[cfg(all(unix, feature = "vt-engine"))]
        commit.finish(&state).await;
        #[cfg(not(all(unix, feature = "vt-engine")))]
        match commit {}
    }
    // Pruning belongs to the socket owner: a start that lost the busy check
    // returned before binding, and a restore reaches here only once committed.
    if let Err(err) = image::prune_images(&images_dir, &running_image) {
        tracing::warn!(error = %err, "gterm image prune failed");
    }

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
                let _gate = state.mutation_gate.read().await;
                state.broadcast_frames().await;
            }
        })
    };

    tokio::select! {
        _ = shutdown_rx.changed() => {}
        _ = terminate.recv() => {
            state.draining.store(true, Ordering::SeqCst);
        }
    }
    // An attempt in flight answers first: one still probing rechecks, finds
    // the host draining, and defers before the sockets go; an accepted one
    // aborts at its next cutoff.
    #[cfg(all(unix, feature = "vt-engine"))]
    let _settled = state.attempts.settled().await;
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
    /// Restore from a handover instead of binding fresh.
    resume: Option<Resume>,
    /// Report whether this build restores that state format, then exit.
    probe_resume: Option<u32>,
}

/// `--resume-state PATH`, with `--resume-fallback` when this restore is the
/// fallback into the earlier image.
#[derive(Debug)]
struct Resume {
    state: PathBuf,
    fallback: bool,
}

impl Resume {
    /// The resume flags in an argv that does not otherwise parse.
    #[cfg(all(unix, feature = "vt-engine"))]
    fn scan(argv: impl IntoIterator<Item = String>) -> Option<Self> {
        let mut state = None;
        let mut fallback = false;
        let mut args = argv.into_iter();
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--resume-state" => state = args.next().map(PathBuf::from),
                "--resume-fallback" => fallback = true,
                _ => {}
            }
        }
        state.map(|state| Self { state, fallback })
    }
}

impl HostArgs {
    /// Parse `gterm host` argv, or print usage and exit (0 for help, 2 for a
    /// bad argument) before anything touches the socket dir (#22425).
    fn parse() -> Self {
        match Self::from_args(std::env::args().skip(2)) {
            // Before tracing, the token, or pinning: a probe touches nothing.
            Ok(Self {
                probe_resume: Some(version),
                ..
            }) => {
                let formats: Vec<String> = RESUMABLE_FORMATS.iter().map(u32::to_string).collect();
                println!("{}", formats.join(","));
                std::process::exit(if RESUMABLE_FORMATS.contains(&version) {
                    0
                } else {
                    3
                });
            }
            Ok(args) => args,
            Err(ArgError::Help) => {
                print!("{HOST_USAGE}");
                std::process::exit(0);
            }
            Err(ArgError::Invalid(message)) => {
                eprintln!("gterm host: {message}");
                // An argv this image rejects may still carry a restore: the
                // earlier image, which wrote that argv, takes it back.
                #[cfg(all(unix, feature = "vt-engine"))]
                if let Some(resume) = Resume::scan(std::env::args().skip(2)) {
                    let (_, fallback) = handover::fallback::begin(&resume.state, resume.fallback);
                    fallback.fail(&io::Error::new(io::ErrorKind::InvalidInput, message));
                }
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
        let mut resume_state = None;
        let mut resume_fallback = false;
        let mut probe_resume = None;
        let mut args = argv.into_iter();
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--help" | "-h" => return Err(ArgError::Help),
                "--socket-dir" => {
                    socket_dir = PathBuf::from(value_for(&arg, &mut args)?);
                }
                "--resume-state" => {
                    resume_state = Some(PathBuf::from(value_for(&arg, &mut args)?));
                }
                "--resume-fallback" => resume_fallback = true,
                "--probe-resume" => {
                    let value = value_for(&arg, &mut args)?;
                    probe_resume = Some(value.parse().map_err(|_| {
                        ArgError::Invalid(format!("`{arg}` needs a format version, got `{value}`"))
                    })?);
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
        let resume = resume_state.map(|state| Resume {
            state,
            fallback: resume_fallback,
        });
        Ok(Self {
            token_file: socket_dir.join(TOKEN_FILE),
            pid_file: socket_dir.join(PID_FILE),
            log_file,
            socket_dir,
            shutdown_grace_ms: 150,
            host_config,
            resume,
            probe_resume,
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
      --resume-state PATH                restore from a handover state file
      --resume-fallback                  the restore is a fallback into the earlier image
      --probe-resume N                   print the restorable state formats; exit 0 if N is one, else 3
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

fn read_api_key() -> Option<String> {
    read_api_key_from(gobby_home().as_deref())
}

fn read_api_key_from(gobby_home: Option<&Path>) -> Option<String> {
    let text = fs::read_to_string(gobby_home?.join("bootstrap.yaml")).ok()?;
    let bootstrap: yaml_serde::Value = yaml_serde::from_str(&text).ok()?;
    let api_key = bootstrap.get("api_key")?.as_str()?.trim();
    (!api_key.is_empty()).then(|| api_key.to_owned())
}

fn dirs_home() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from)
}

/// Gobby home for this host, honouring `GOBBY_HOME` exactly as the daemon does.
///
/// An isolated daemon sets `GOBBY_HOME` and writes its bootstrap API key there. Falling
/// straight through to `~/.gobby` would make the host expect the machine-wide operator
/// key and reject the credential its own daemon sends.
fn gobby_home() -> Option<PathBuf> {
    if let Some(configured) = std::env::var_os("GOBBY_HOME") {
        let path = PathBuf::from(configured);
        if !path.as_os_str().is_empty() {
            return Some(path);
        }
    }
    dirs_home().map(|home| home.join(".gobby"))
}

/// Returns the pin this host runs from. A host launched from any other path
/// pins that binary and re-execs the pin with the same argv and environment,
/// before any socket is touched, so promotion never replaces its bytes.
fn run_from_pin(images_dir: &Path) -> io::Result<image::PinnedImage> {
    use std::os::unix::process::CommandExt;

    // A symlink to the pin is the pin; pinned_image judges the path it names.
    let exe = fs::canonicalize(std::env::current_exe()?)?;
    if let Some(pin) = image::pinned_image(images_dir, &exe)? {
        return Ok(pin);
    }
    let pin = image::pin_image(images_dir, &exe)?;
    pin.verify()?;
    if fs::canonicalize(&exe)? == fs::canonicalize(&pin.path)? {
        return Err(io::Error::other(format!(
            "{} is the pin but does not verify as one",
            pin.path.display()
        )));
    }
    let mut argv = std::env::args_os();
    let arg0 = argv
        .next()
        .unwrap_or_else(|| pin.path.clone().into_os_string());
    Err(std::process::Command::new(&pin.path)
        .arg0(arg0)
        .args(argv)
        .exec())
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

//! Independent `gclient` startup: discover the daemon, probe health, then TUI.

use crate::frame_source::FrameDelivery;
use crate::prefs::{load_prefs, prefs_path, PREFS_FILE};
use crate::teardown::{CrosstermBackend, ModeBackend, TerminalGuard};
use crate::ui::keymap::{default_override_path, default_prefix, Keymap};
use crate::ui::settings::ClientPrefs;
use gobby_terminal::protocol::PROTOCOL_VERSION;
use serde::Deserialize;
use std::path::{Path, PathBuf};
use std::time::Duration;
use thiserror::Error;

const HEALTH_PATH: &str = "/api/health";
const HEALTH_TIMEOUT: Duration = Duration::from_secs(2);
const USAGE: &str = "Usage: gclient [--project PROJECT] [--node NODE] [--workspace WORKSPACE] \
     [--daemon-url URL] [--token-file PATH] [--frame-delivery auto|direct|proxy] \
     [--no-mouse] [--version]";
const FRAME_DELIVERY_USAGE: &str = "--frame-delivery requires auto, direct, or proxy";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliArgs {
    pub project: Option<String>,
    /// `--node`: the node whose workspace to attach; `None` is the local node.
    pub node: Option<String>,
    /// `--workspace`: a `[n#:]w#` ref or workspace name; `None` is `default`.
    pub workspace: Option<String>,
    pub daemon_url: Option<String>,
    pub token_file: Option<PathBuf>,
    pub frame_delivery: FrameDelivery,
    pub no_mouse: bool,
    pub version: bool,
    pub help: bool,
}

/// The workspace a window attaches to: what `workspace_attach` sends, with
/// absent fields left to the daemon (the local node, its `default`).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct AttachTarget {
    pub node: Option<String>,
    pub workspace: Option<String>,
}

impl AttachTarget {
    /// Combine `--node` and `--workspace`; a full `node:workspace` ref carries
    /// its own node, which wins over `--node`.
    pub fn from_flags(node: Option<&str>, workspace: Option<&str>) -> Self {
        let mut target = Self {
            node: node.map(str::to_string),
            workspace: workspace.map(str::to_string),
        };
        if let Some((head, rest)) = workspace.and_then(|reference| reference.split_once(':')) {
            // Refs are letterless and left-anchored: `2:1` is workspace 1 on
            // node 2, so a numeric head names the node.
            let is_node_ref = !head.is_empty() && head.bytes().all(|b| b.is_ascii_digit());
            if is_node_ref {
                target.node = Some(head.to_string());
                target.workspace = Some(rest.to_string());
            }
        }
        target
    }
}

#[derive(Debug, Clone)]
pub struct ProbeEnv {
    pub daemon_url: String,
    pub token: Option<String>,
    /// An outer tmux client owns this terminal, so its prefix eats `ctrl+b`.
    pub nested_tmux: bool,
    /// gclient was started inside a gclient pane (`GOBBY_PANE_ID` is set):
    /// the outer window owns the terminal and its prefix.
    pub in_pane: bool,
}

impl ProbeEnv {
    /// An outer client owns the terminal, so the prefix shifts to
    /// [`default_prefix`]`(true)`.
    pub fn nested(&self) -> bool {
        self.nested_tmux || self.in_pane
    }
}

#[derive(Debug, Clone)]
pub struct Ready {
    pub daemon_url: String,
    pub token: Option<String>,
    /// The project named by `--project` or found above the launch
    /// directory; none when gclient starts outside every checkout.
    pub project: Option<String>,
    /// Where gclient was started: the shell of a project without a local
    /// checkout starts here.
    pub launch_dir: PathBuf,
    pub frame_delivery: FrameDelivery,
    pub host: Option<GtermHostState>,
    pub host_notice: Option<String>,
    pub prefs: ClientPrefs,
    pub keymap: Keymap,
    /// The prefix is shifted because an outer client owns the terminal
    /// (see [`ProbeEnv::nested`]); the keymap was built for it.
    pub nested: bool,
    /// See [`ProbeEnv::in_pane`]: the window opens no terminal of its own.
    pub in_pane: bool,
    pub gobby_home: PathBuf,
    /// The workspace the window attaches to, from `--node`/`--workspace`.
    pub attach: AttachTarget,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GtermHostState {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub running: bool,
    #[serde(default)]
    pub adopted: bool,
    pub host_epoch: Option<String>,
    pub protocol_version: Option<u32>,
    #[serde(default)]
    pub restart_count: u64,
    #[serde(default)]
    pub backoff_seconds: f64,
    pub last_error: Option<String>,
}

#[derive(Debug, Deserialize)]
struct HealthPayload {
    #[serde(default)]
    gterm_host: Option<GtermHostState>,
}

#[derive(Debug, Error)]
pub enum StartupError {
    #[error("{message}")]
    Usage { message: String },
    #[error(
        "daemon unreachable at {url}: {detail}\nStart it with `gobby start` and check `gobby status`."
    )]
    Unreachable { url: String, detail: String },
    #[error(
        "failed to read daemon token from {path}: {detail}\n\
         Pass a readable token file with `--token-file PATH`."
    )]
    TokenFile { path: String, detail: String },
    #[error(
        "could not resolve a Gobby project from {search_dir}: {detail}\n\
         Run `gobby init` in the project root or pass `--project UUID_OR_PATH`."
    )]
    Project { search_dir: String, detail: String },
    #[error(
        "gterm host is unusable: running={running} adopted={adopted} epoch={epoch} \
         protocol_version={protocol_version} expected_protocol_version={expected_protocol_version} \
         restart_count={restart_count} backoff_seconds={backoff_seconds} \
         last_error={last_error}\n\
         Native terminals require a running compatible host. Check `gobby status`."
    )]
    DegradedHost {
        running: bool,
        adopted: bool,
        epoch: String,
        protocol_version: String,
        expected_protocol_version: u32,
        restart_count: u64,
        backoff_seconds: f64,
        last_error: String,
    },
    #[error(
        "failed to load client prefs from {path}: {detail}\n\
         Every key in the file is optional; fix or remove the offending line."
    )]
    Prefs { path: String, detail: String },
    #[error(
        "failed to load keymap overrides from {path}: {detail}\n\
         Fix or remove the offending binding; a missing file is the default keymap."
    )]
    Keymap { path: String, detail: String },
    #[error("terminal mode: {0}")]
    Terminal(#[from] std::io::Error),
}

pub trait HealthClient {
    fn fetch_health(&self, daemon_url: &str) -> Result<Option<GtermHostState>, StartupError>;
}

pub struct HttpHealthClient {
    timeout: Duration,
}

impl HttpHealthClient {
    pub fn new() -> Self {
        Self {
            timeout: HEALTH_TIMEOUT,
        }
    }
}

impl Default for HttpHealthClient {
    fn default() -> Self {
        Self::new()
    }
}

impl HealthClient for HttpHealthClient {
    fn fetch_health(&self, daemon_url: &str) -> Result<Option<GtermHostState>, StartupError> {
        let base = daemon_url.trim_end_matches('/');
        let url = format!("{base}{HEALTH_PATH}");
        match http_get_health(&url, self.timeout) {
            Ok(payload) => Ok(payload.gterm_host),
            Err(detail) => Err(StartupError::Unreachable {
                url: base.to_string(),
                detail,
            }),
        }
    }
}

fn http_get_health(url: &str, timeout: Duration) -> Result<HealthPayload, String> {
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|err| err.to_string())?;
    rt.block_on(async {
        let client = reqwest::Client::builder()
            .timeout(timeout)
            .connect_timeout(timeout)
            .build()
            .map_err(|err| err.to_string())?;
        let response = client
            .get(url)
            .send()
            .await
            .map_err(|err| err.to_string())?;
        if !response.status().is_success() {
            return Err(format!("HTTP {}", response.status()));
        }
        response.json().await.map_err(|err| err.to_string())
    })
}

pub fn parse_args<I, S>(args: I) -> Result<CliArgs, StartupError>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut iter = args.into_iter();
    let _argv0 = iter.next();
    let mut project = None;
    let mut node = None;
    let mut workspace = None;
    let mut daemon_url = None;
    let mut token_file = None;
    let mut frame_delivery = FrameDelivery::default();
    let mut no_mouse = false;
    let mut version = false;
    let mut help = false;
    while let Some(raw) = iter.next() {
        let arg = raw.as_ref();
        if let Some(value) = arg.strip_prefix("--project=") {
            if value.is_empty() {
                return Err(StartupError::Usage {
                    message: "--project requires a workspace".into(),
                });
            }
            project = Some(value.to_string());
            continue;
        }
        if arg == "--project" {
            let value = iter.next().ok_or_else(|| StartupError::Usage {
                message: "--project requires a workspace".into(),
            })?;
            let value = value.as_ref();
            if value.is_empty() || value.starts_with('-') {
                return Err(StartupError::Usage {
                    message: "--project requires a workspace".into(),
                });
            }
            project = Some(value.to_string());
            continue;
        }
        if let Some(value) = arg.strip_prefix("--node=") {
            if value.is_empty() {
                return Err(StartupError::Usage {
                    message: "--node requires a node ref".into(),
                });
            }
            node = Some(value.to_string());
            continue;
        }
        if arg == "--node" {
            let value = iter.next().ok_or_else(|| StartupError::Usage {
                message: "--node requires a node ref".into(),
            })?;
            let value = value.as_ref();
            if value.is_empty() || value.starts_with('-') {
                return Err(StartupError::Usage {
                    message: "--node requires a node ref".into(),
                });
            }
            node = Some(value.to_string());
            continue;
        }
        if let Some(value) = arg.strip_prefix("--workspace=") {
            if value.is_empty() {
                return Err(StartupError::Usage {
                    message: "--workspace requires a workspace ref".into(),
                });
            }
            workspace = Some(value.to_string());
            continue;
        }
        if arg == "--workspace" {
            let value = iter.next().ok_or_else(|| StartupError::Usage {
                message: "--workspace requires a workspace ref".into(),
            })?;
            let value = value.as_ref();
            if value.is_empty() || value.starts_with('-') {
                return Err(StartupError::Usage {
                    message: "--workspace requires a workspace ref".into(),
                });
            }
            workspace = Some(value.to_string());
            continue;
        }
        if arg == "--version" || arg == "-V" {
            version = true;
            continue;
        }
        if let Some(value) = arg.strip_prefix("--daemon-url=") {
            if value.is_empty() {
                return Err(StartupError::Usage {
                    message: "--daemon-url requires a URL".into(),
                });
            }
            daemon_url = Some(value.to_string());
            continue;
        }
        if arg == "--daemon-url" {
            let value = iter.next().ok_or_else(|| StartupError::Usage {
                message: "--daemon-url requires a URL".into(),
            })?;
            let value = value.as_ref();
            if value.is_empty() || value.starts_with('-') {
                return Err(StartupError::Usage {
                    message: "--daemon-url requires a URL".into(),
                });
            }
            daemon_url = Some(value.to_string());
            continue;
        }
        if let Some(value) = arg.strip_prefix("--token-file=") {
            if value.is_empty() {
                return Err(StartupError::Usage {
                    message: "--token-file requires a path".into(),
                });
            }
            token_file = Some(PathBuf::from(value));
            continue;
        }
        if arg == "--token-file" {
            let value = iter.next().ok_or_else(|| StartupError::Usage {
                message: "--token-file requires a path".into(),
            })?;
            let value = value.as_ref();
            if value.is_empty() || value.starts_with('-') {
                return Err(StartupError::Usage {
                    message: "--token-file requires a path".into(),
                });
            }
            token_file = Some(PathBuf::from(value));
            continue;
        }
        if let Some(value) = arg.strip_prefix("--frame-delivery=") {
            frame_delivery = parse_frame_delivery(value)?;
            continue;
        }
        if arg == "--frame-delivery" {
            let value = iter.next().ok_or_else(|| StartupError::Usage {
                message: FRAME_DELIVERY_USAGE.into(),
            })?;
            frame_delivery = parse_frame_delivery(value.as_ref())?;
            continue;
        }
        if arg == "--no-mouse" {
            no_mouse = true;
            continue;
        }
        if arg == "--help" || arg == "-h" {
            help = true;
            continue;
        }
        return Err(StartupError::Usage {
            message: format!("unknown argument: {arg}"),
        });
    }
    Ok(CliArgs {
        project,
        node,
        workspace,
        daemon_url,
        token_file,
        frame_delivery,
        no_mouse,
        version,
        help,
    })
}

/// The value set is closed, so this also rejects a missing value that swallowed
/// the next flag — no separate `starts_with('-')` guard is needed.
fn parse_frame_delivery(value: &str) -> Result<FrameDelivery, StartupError> {
    FrameDelivery::parse(value).ok_or_else(|| StartupError::Usage {
        message: FRAME_DELIVERY_USAGE.into(),
    })
}

pub fn resolve_probe_env_at(
    args: &CliArgs,
    default_daemon_url: &str,
    default_token_file: &Path,
    nested_tmux: bool,
    in_pane: bool,
) -> Result<ProbeEnv, StartupError> {
    let daemon_url = args
        .daemon_url
        .clone()
        .unwrap_or_else(|| default_daemon_url.to_string());
    let token_file = args.token_file.as_deref().unwrap_or(default_token_file);
    let token = std::fs::read_to_string(token_file).map_err(|err| StartupError::TokenFile {
        path: token_file.display().to_string(),
        detail: err.to_string(),
    })?;
    let token = token.trim();
    if token.is_empty() {
        return Err(StartupError::TokenFile {
            path: token_file.display().to_string(),
            detail: "file is empty".into(),
        });
    }
    Ok(ProbeEnv {
        daemon_url,
        token: Some(token.to_string()),
        nested_tmux,
        in_pane,
    })
}

fn resolve_probe_env(args: &CliArgs) -> Result<ProbeEnv, StartupError> {
    let default_daemon_url = gobby_core::daemon_url::daemon_url();
    let default_token_file = match args.token_file.as_ref() {
        Some(path) => path.clone(),
        None => gobby_core::gobby_home()
            .map_err(|err| StartupError::TokenFile {
                path: "~/.gobby/local_cli_token".into(),
                detail: err.to_string(),
            })?
            .join("local_cli_token"),
    };
    resolve_probe_env_at(
        args,
        &default_daemon_url,
        &default_token_file,
        crate::tmux_identity::current().is_some(),
        std::env::var_os("GOBBY_PANE_ID").is_some_and(|id| !id.is_empty()),
    )
}

pub fn prepare(
    args: &CliArgs,
    env: ProbeEnv,
    health: &impl HealthClient,
) -> Result<Ready, StartupError> {
    let current_dir = std::env::current_dir().map_err(|err| StartupError::Project {
        search_dir: "current directory".into(),
        detail: err.to_string(),
    })?;
    let gobby_home = gobby_core::gobby_home().map_err(|err| StartupError::Prefs {
        path: format!("~/.gobby/{PREFS_FILE}"),
        detail: err.to_string(),
    })?;
    prepare_at(args, env, health, &current_dir, &gobby_home)
}

pub fn prepare_at(
    args: &CliArgs,
    env: ProbeEnv,
    health: &impl HealthClient,
    current_dir: &Path,
    gobby_home: &Path,
) -> Result<Ready, StartupError> {
    let project = resolve_project_at(args.project.as_deref(), current_dir)?;
    let mut prefs = load_prefs(gobby_home).map_err(|error| StartupError::Prefs {
        path: prefs_path(gobby_home).display().to_string(),
        detail: error.to_string(),
    })?;
    if args.no_mouse {
        prefs.mouse_capture = false;
    }
    let keymap = load_keymap(&prefs, gobby_home, env.nested())?;
    // A stopped daemon is a wait, not an exit: the loop's supervisor connects
    // once it is back, so the probe's report becomes the launch notice. Every
    // other probe failure still ends the launch before raw mode.
    let (host, host_notice) = match health.fetch_health(&env.daemon_url) {
        Ok(host) => {
            if !host
                .as_ref()
                .is_some_and(|host| host.running && host.protocol_version == Some(PROTOCOL_VERSION))
            {
                return Err(degraded_host(host.as_ref()));
            }
            let notice = host.as_ref().and_then(host_notice);
            (host, notice)
        }
        Err(error @ StartupError::Unreachable { .. }) => (None, Some(error.to_string())),
        Err(error) => return Err(error),
    };
    let nested = env.nested();
    Ok(Ready {
        daemon_url: env.daemon_url,
        token: env.token,
        project,
        frame_delivery: args.frame_delivery,
        host,
        host_notice,
        prefs,
        keymap,
        nested,
        in_pane: env.in_pane,
        gobby_home: gobby_home.to_path_buf(),
        launch_dir: current_dir.to_path_buf(),
        attach: AttachTarget::from_flags(args.node.as_deref(), args.workspace.as_deref()),
    })
}

/// The project the workspace opens on: the resolved one, else the project
/// the last run left focused, else the personal project (every daemon has
/// it; its shells start in the launch directory).
pub fn initial_project(resolved: Option<String>, focused: Option<&str>) -> String {
    resolved
        .or_else(|| focused.map(str::to_string))
        .unwrap_or_else(|| gobby_core::project::PERSONAL_PROJECT_ID.to_string())
}

/// The keymap override file: `[keymap] path` from prefs when set (a relative
/// path resolves under the gobby home), else the client-local default.
pub fn keymap_override_path(prefs: &ClientPrefs, gobby_home: &Path) -> PathBuf {
    if prefs.keybinds.is_empty() {
        default_override_path(gobby_home)
    } else {
        gobby_home.join(&prefs.keybinds)
    }
}

/// Load the keymap for these prefs: the defaults behind this launch's prefix
/// (see [`ProbeEnv::nested`]) merged with the override file when it
/// exists. A rejected file names its path and the error.
pub fn load_keymap(
    prefs: &ClientPrefs,
    gobby_home: &Path,
    nested: bool,
) -> Result<Keymap, StartupError> {
    let path = keymap_override_path(prefs, gobby_home);
    Keymap::load_overrides(&path, default_prefix(nested)).map_err(|error| StartupError::Keymap {
        path: path.display().to_string(),
        detail: error.to_string(),
    })
}

/// The project for this launch: an explicit `--project` must resolve; a
/// checkout above `current_dir` is used when present, and a directory
/// outside every checkout resolves to none (the client still starts).
pub fn resolve_project_at(
    project: Option<&str>,
    current_dir: &Path,
) -> Result<Option<String>, StartupError> {
    if let Some(project) = project {
        if uuid::Uuid::parse_str(project).is_ok() {
            return Ok(Some(project.to_string()));
        }
        let root = Path::new(project);
        let root = if root.is_absolute() {
            root.to_path_buf()
        } else {
            current_dir.join(root)
        };
        return gobby_core::project::read_project_id(&root)
            .map(Some)
            .map_err(|err| StartupError::Project {
                search_dir: current_dir.display().to_string(),
                detail: format!("failed to read project id from {}: {err}", root.display()),
            });
    }

    let Some(root) = gobby_core::project::find_project_root(current_dir) else {
        return Ok(None);
    };
    gobby_core::project::read_project_id(&root)
        .map(Some)
        .map_err(|err| StartupError::Project {
            search_dir: current_dir.display().to_string(),
            detail: format!("failed to read project id from {}: {err}", root.display()),
        })
}

fn degraded_host(host: Option<&GtermHostState>) -> StartupError {
    StartupError::DegradedHost {
        running: host.is_some_and(|host| host.running),
        adopted: host.is_some_and(|host| host.adopted),
        epoch: host
            .and_then(|host| host.host_epoch.clone())
            .unwrap_or_else(|| "none".to_string()),
        protocol_version: host
            .and_then(|host| host.protocol_version)
            .map(|version| version.to_string())
            .unwrap_or_else(|| "none".to_string()),
        expected_protocol_version: PROTOCOL_VERSION,
        restart_count: host.map_or(0, |host| host.restart_count),
        backoff_seconds: host.map_or(0.0, |host| host.backoff_seconds),
        last_error: host
            .and_then(|host| host.last_error.clone())
            .unwrap_or_else(|| "none".to_string()),
    }
}

fn host_notice(host: &GtermHostState) -> Option<String> {
    host.last_error.as_ref().map(|last_error| {
        format!(
            "gterm host notice: last_error={last_error} restart_count={} backoff_seconds={}",
            host.restart_count, host.backoff_seconds
        )
    })
}

pub fn start_session<B: ModeBackend>(
    args: CliArgs,
    env: ProbeEnv,
    health: &impl HealthClient,
    backend: B,
) -> Result<(Ready, TerminalGuard<B>), StartupError> {
    let ready = prepare(&args, env, health)?;
    arm_session(ready, backend)
}

/// `start_session` with an explicit working directory and gobby home, for callers
/// that must not touch the real `~/.gobby`.
pub fn start_session_at<B: ModeBackend>(
    args: CliArgs,
    env: ProbeEnv,
    health: &impl HealthClient,
    backend: B,
    current_dir: &Path,
    gobby_home: &Path,
) -> Result<(Ready, TerminalGuard<B>), StartupError> {
    let ready = prepare_at(&args, env, health, current_dir, gobby_home)?;
    arm_session(ready, backend)
}

fn arm_session<B: ModeBackend>(
    ready: Ready,
    backend: B,
) -> Result<(Ready, TerminalGuard<B>), StartupError> {
    // Before raw mode, so the scrollback keeps the notice once the
    // alternate screen takes the terminal.
    if let Some(notice) = &ready.host_notice {
        eprintln!("{notice}");
    }
    let mut guard = TerminalGuard::new(backend);
    guard.arm(ready.prefs.mouse_capture)?;
    Ok((ready, guard))
}

pub fn run() -> anyhow::Result<()> {
    let args = parse_args(std::env::args())?;
    if args.version {
        println!("gclient {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if args.help {
        println!("{USAGE}");
        return Ok(());
    }
    if let Err(error) = crate::logging::init() {
        eprintln!("failed to initialize gclient logging: {error}");
    }
    let env = resolve_probe_env(&args)?;
    let health = HttpHealthClient::new();
    let (ready, mut guard) = start_session(args, env, &health, CrosstermBackend)?;
    crate::views::run_ready(ready, &mut guard)
}

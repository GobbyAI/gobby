//! Independent `gclient` startup: discover the daemon, probe health, then TUI.

use crate::teardown::{CrosstermBackend, ModeBackend, TerminalGuard};
use serde::Deserialize;
use std::io::IsTerminal;
use std::time::Duration;
use thiserror::Error;

const HEALTH_PATH: &str = "/api/admin/health";
const HEALTH_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CliArgs {
    pub project: Option<String>,
}

#[derive(Debug, Clone)]
pub struct ProbeEnv {
    pub daemon_url: String,
    pub token: Option<String>,
}

#[derive(Debug, Clone)]
pub struct Ready {
    pub daemon_url: String,
    pub token: Option<String>,
    pub project: Option<String>,
    pub host: Option<GtermHostState>,
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
    #[serde(default)]
    pub restart_count: u64,
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
        "gterm host is down: running={running} adopted={adopted} epoch={epoch} \
         restart_count={restart_count} last_error={last_error}\n\
         Native terminals cannot start until the host is healthy. Check `gobby status`."
    )]
    DegradedHost {
        running: bool,
        adopted: bool,
        epoch: String,
        restart_count: u64,
        last_error: String,
    },
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
        if arg == "--help" || arg == "-h" {
            return Err(StartupError::Usage {
                message: "Usage: gclient [--project PROJECT]".into(),
            });
        }
        return Err(StartupError::Usage {
            message: format!("unknown argument: {arg}"),
        });
    }
    Ok(CliArgs { project })
}

pub fn prepare(
    args: &CliArgs,
    env: ProbeEnv,
    health: &impl HealthClient,
) -> Result<Ready, StartupError> {
    let host = health.fetch_health(&env.daemon_url)?;
    if let Some(host) = host.as_ref() {
        if host.enabled && !host.running {
            return Err(degraded_host(host));
        }
    }
    Ok(Ready {
        daemon_url: env.daemon_url,
        token: env.token,
        project: args.project.clone(),
        host,
    })
}

fn degraded_host(host: &GtermHostState) -> StartupError {
    StartupError::DegradedHost {
        running: host.running,
        adopted: host.adopted,
        epoch: host
            .host_epoch
            .clone()
            .unwrap_or_else(|| "none".to_string()),
        restart_count: host.restart_count,
        last_error: host
            .last_error
            .clone()
            .unwrap_or_else(|| "none".to_string()),
    }
}

pub fn start_session<B: ModeBackend>(
    args: CliArgs,
    env: ProbeEnv,
    health: &impl HealthClient,
    backend: B,
) -> Result<(Ready, TerminalGuard<B>), StartupError> {
    let ready = prepare(&args, env, health)?;
    let mut guard = TerminalGuard::new(backend);
    guard.arm()?;
    Ok((ready, guard))
}

pub fn run() -> anyhow::Result<()> {
    let args = parse_args(std::env::args())?;
    let env = ProbeEnv {
        daemon_url: gobby_core::daemon_url::daemon_url(),
        token: gobby_core::local_token::read_local_cli_token().ok(),
    };
    let health = HttpHealthClient::new();
    if std::io::stdout().is_terminal() {
        let (ready, _guard) = start_session(args, env, &health, CrosstermBackend)?;
        return crate::views::run_ready(ready);
    }
    let ready = prepare(&args, env, &health)?;
    crate::views::run_ready(ready)
}

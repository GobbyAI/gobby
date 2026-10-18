//! Blocking HTTP dialer, shared across blocking-pool requests.

use crate::{contract::WRAPPER_CONTRACT, terminal_context};
use anyhow::Context;
use gobby_core::{daemon_url::daemon_url, local_token, project};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::error::Error;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};

type Headers = BTreeMap<String, String>;

struct CachedState {
    auth: Headers,
    caller_project: Option<String>,
    last_preflight: Option<(String, Instant)>,
}

/// One process's identity and caches. No state lock spans HTTP or preflight I/O.
pub struct Dialer {
    cwd: PathBuf,
    environment_session: Option<String>,
    terminal_context: String,
    has_tmux_pane: bool,
    agent: ureq::Agent,
    state: Mutex<CachedState>,
}

pub struct RequestOptions<'a> {
    pub project_id: Option<&'a str>,
    pub session_id: Option<&'a str>,
    pub timeout: Duration,
    pub preflight: bool,
    pub params: &'a [(&'a str, &'a str)],
}

impl Default for RequestOptions<'_> {
    fn default() -> Self {
        Self {
            project_id: None,
            session_id: None,
            timeout: Duration::from_secs(30),
            preflight: false,
            params: &[],
        }
    }
}

impl Dialer {
    pub fn new() -> anyhow::Result<Self> {
        Self::with_context(&std::env::current_dir()?, terminal_context::capture())
    }

    /// Capture identity for an explicit working directory and terminal context.
    pub fn with_context(cwd: &Path, terminal: Map<String, Value>) -> anyhow::Result<Self> {
        Ok(Self {
            cwd: cwd.to_owned(),
            environment_session: env_value("GOBBY_SESSION_ID"),
            terminal_context: terminal_context::serialize(&terminal),
            has_tmux_pane: terminal
                .get("tmux_pane")
                .is_some_and(|v| v.as_str().is_some_and(|s| !s.is_empty())),
            agent: ureq::AgentBuilder::new().redirects(0).build(),
            state: Mutex::new(CachedState {
                auth: auth_headers()?,
                caller_project: caller_project(cwd),
                last_preflight: None,
            }),
        })
    }

    pub fn request(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
        options: RequestOptions<'_>,
    ) -> Value {
        match self.request_inner(method, path, body, &options) {
            Ok(value) => value,
            Err(error) => json!({"success": false, "error": error.to_string()}),
        }
    }

    fn cached(&self) -> anyhow::Result<MutexGuard<'_, CachedState>> {
        self.state
            .lock()
            .map_err(|_| anyhow::anyhow!("dialer cache lock poisoned"))
    }

    fn request_inner(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
        options: &RequestOptions<'_>,
    ) -> anyhow::Result<Value> {
        // A restart may change the port; resolve the endpoint on every request.
        let base = daemon_url()?.trim_end_matches('/').to_owned();
        if options.preflight {
            let recent = self
                .cached()?
                .last_preflight
                .as_ref()
                .is_some_and(|(url, when)| url == &base && when.elapsed() < Duration::from_secs(5));
            if !recent {
                let response = self
                    .agent
                    .get(&format!("{base}/api/health"))
                    .timeout(Duration::from_secs(2))
                    .call();
                match response {
                    Ok(response) if response.status() == 200 => {
                        self.cached()?.last_preflight = Some((base.clone(), Instant::now()));
                    }
                    Err(ureq::Error::Status(503, response)) => {
                        let text = response.into_string()?;
                        if let Some(detail) = backend_unavailable(&text) {
                            return Ok(unavailable(&base, &detail));
                        }
                        return Ok(unavailable(
                            &base,
                            "health check could not connect within 2s or got an unhealthy answer",
                        ));
                    }
                    // Busy read timeouts are tolerated but never cached as success.
                    Err(ureq::Error::Transport(error)) if is_timeout(&error) => {}
                    _ => {
                        return Ok(unavailable(
                            &base,
                            "health check could not connect within 2s or got an unhealthy answer",
                        ));
                    }
                }
            }
        }
        let caller = self
            .cached()?
            .caller_project
            .clone()
            .or_else(|| caller_project(&self.cwd));
        if caller.is_some() {
            self.cached()?.caller_project = caller.clone();
        }
        let mut headers = Headers::from([(
            WRAPPER_CONTRACT.protocol_version_header.clone(),
            WRAPPER_CONTRACT.protocol_version.clone(),
        )]);
        if let Some(project) = options
            .project_id
            .filter(|id| !id.is_empty())
            .or(caller.as_deref())
        {
            headers.insert("X-Gobby-Project-Id".into(), project.into());
        }
        if let Some(caller) = caller {
            headers.insert("X-Gobby-Caller-Project-Id".into(), caller);
        }
        let session = self.environment_session.as_deref().or_else(|| {
            options
                .session_id
                .filter(|id| !id.is_empty() && !self.has_tmux_pane)
        });
        if let Some(session) = session {
            headers.insert("X-Gobby-Session-Id".into(), session.into());
        } else {
            headers.insert(
                "X-Gobby-Terminal-Context".into(),
                self.terminal_context.clone(),
            );
        }
        if let Some(run) = env_value("GOBBY_AGENT_RUN_ID") {
            headers.insert("X-Gobby-Agent-Run-Id".into(), run);
        }
        if std::env::var_os("GOBBY_MANAGED_EXECUTION_BOOTSTRAP").is_some() {
            let fresh = auth_headers()?;
            self.cached()?.auth = fresh;
        }
        let mut auth = self.cached()?.auth.clone();
        let mut response = self.send(
            method,
            &format!("{base}{path}"),
            body,
            options,
            &headers,
            &auth,
        );
        if matches!(&response, Ok(response) if response.status() == 401) {
            auth = auth_headers()?;
            self.cached()?.auth = auth.clone();
            response = self.send(
                method,
                &format!("{base}{path}"),
                body,
                options,
                &headers,
                &auth,
            );
        }
        let response = match response {
            Ok(response) => response,
            Err(error) => {
                self.cached()?.last_preflight = None;
                return Ok(if is_timeout(error.as_ref()) {
                    request_timeout(path, options.timeout)
                } else {
                    unavailable(&base, "connection failed")
                });
            }
        };
        let status = response.status();
        let text = match response.into_string() {
            Ok(text) => text,
            Err(error) if is_timeout(&error) => return Ok(request_timeout(path, options.timeout)),
            Err(error) => return Err(error.into()),
        };
        if status == 200 {
            return serde_json::from_str(&text).context("invalid daemon JSON response");
        }
        if status == 503
            && let Some(detail) = backend_unavailable(&text)
        {
            return Ok(unavailable(&base, &detail));
        }
        if status == 409
            && let Ok(value) = serde_json::from_str::<Value>(&text)
            && value["detail"].is_object()
            && value["detail"]["error_code"] == "SESSION_REQUIRED"
        {
            return Ok(value["detail"].clone());
        }
        Ok(json!({"success": false, "error": format!("HTTP {status}: {text}")}))
    }

    fn send(
        &self,
        method: &str,
        url: &str,
        body: Option<&Value>,
        options: &RequestOptions<'_>,
        headers: &Headers,
        auth: &Headers,
    ) -> Result<ureq::Response, Box<ureq::Transport>> {
        let mut request = self.agent.request(method, url).timeout(options.timeout);
        for (name, value) in headers.iter().chain(auth) {
            request = request.set(name, value);
        }
        for (name, value) in options.params {
            request = request.query(name, value);
        }
        let response = match body {
            Some(body) => request.send_json(body),
            None => request.call(),
        };
        // HTTP error statuses still carry responses for the wrapper to interpret.
        match response {
            Ok(response) | Err(ureq::Error::Status(_, response)) => Ok(response),
            Err(ureq::Error::Transport(error)) => Err(Box::new(error)),
        }
    }
}

fn env_value(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|value| !value.is_empty())
}

fn caller_project(cwd: &Path) -> Option<String> {
    project::find_project_root(cwd).and_then(|root| project::read_project_id(&root).ok())
}

fn auth_headers() -> anyhow::Result<Headers> {
    let token = local_token::read_api_key()?;
    let mut headers = Headers::from([(
        "Authorization".into(),
        local_token::authorization_bearer(&token),
    )]);
    let agent_token = std::env::var_os("GOBBY_MANAGED_EXECUTION_BOOTSTRAP").is_some()
        || env_value(local_token::AGENT_API_TOKEN_ENV)
            .is_some_and(|value| !value.trim().is_empty());
    if agent_token {
        for (env_name, header) in &WRAPPER_CONTRACT.agent_identity_env_headers {
            if let Some(value) = env_value(env_name)
                .map(|v| v.trim().to_owned())
                .filter(|v| !v.is_empty())
            {
                headers.insert(header.clone(), value);
            }
        }
    }
    Ok(headers)
}

fn backend_unavailable(text: &str) -> Option<String> {
    let value: Value = serde_json::from_str(text).ok()?;
    (value["status"] == "unavailable").then(|| {
        format!(
            "backend {}",
            value["backend"]["state"].as_str().unwrap_or("unknown")
        )
    })
}

fn unavailable(base: &str, detail: &str) -> Value {
    json!({"success":false, "error_code":"DAEMON_UNAVAILABLE", "error":format!("Gobby daemon HTTP control plane is unavailable at {base}: {detail}. Start it with `gobby start`, or check `gobby status`.")})
}

fn request_timeout(path: &str, timeout: Duration) -> Value {
    let hint = if path.trim_end_matches('/').ends_with("/tools/spawn_agent") {
        " Retry spawn_agent with the same task_id to recover the accepted run, or call list_running_agents with that task_id."
    } else {
        ""
    };
    json!({"success":false, "error_code":"REQUEST_TIMEOUT", "error":format!("Gobby daemon request timed out after {}s while calling {path}.{hint}", timeout.as_secs_f64())})
}

fn is_timeout(error: &(dyn Error + 'static)) -> bool {
    let mut current = Some(error);
    while let Some(error) = current {
        if let Some(error) = error.downcast_ref::<io::Error>()
            && matches!(
                error.kind(),
                io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
            )
        {
            return true;
        }
        current = error.source();
    }
    false
}

use std::io;
use std::path::{Path, PathBuf};
use std::time::Duration;

use gobby_core::grant::{
    AGENT_RUN_HEADER, CALLER_PROJECT_HEADER, MANAGED_EXECUTION_HEADER, SESSION_HEADER,
    TARGET_PROJECT_HEADER, reject_remote_endpoint,
};
use gobby_core::local_token::apply_bearer_header;
use serde::Serialize;
use serde_json::Value;

use crate::cli_error::CliError;

const REQUEST_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Debug, Serialize)]
pub(crate) struct StartAskRequest<'a> {
    pub(crate) question: &'a str,
    pub(crate) project_id: &'a str,
    pub(crate) commit_ref: &'a str,
    pub(crate) timeout_seconds: u64,
    pub(crate) retrieval_mode: &'a str,
}

pub(crate) struct AskClient {
    base_url: String,
    project_id: String,
}

impl AskClient {
    pub(crate) fn new(project_id: String) -> Result<Self, CliError> {
        let base_url = gobby_core::daemon_url::daemon_url();
        reject_remote_endpoint(&base_url).map_err(CliError::grant)?;
        Ok(Self {
            base_url: base_url.trim_end_matches('/').to_owned(),
            project_id,
        })
    }

    pub(crate) fn project_id(&self) -> &str {
        &self.project_id
    }

    pub(crate) fn start(&self, request: &StartAskRequest<'_>) -> Result<Value, CliError> {
        let url = format!("{}/api/ask/runs", self.base_url);
        let response = self
            .authenticated(ureq::post(&url).timeout(REQUEST_TIMEOUT))
            .send_json(request);
        json_response(response, "start")
    }

    pub(crate) fn get(&self, run_id: &str) -> Result<Value, CliError> {
        let url = self.run_url(run_id, "");
        let response = self
            .authenticated(ureq::get(&url).timeout(REQUEST_TIMEOUT))
            .call();
        json_response(response, "status")
    }

    pub(crate) fn wait(
        &self,
        run_id: &str,
        timeout_seconds: Option<u64>,
    ) -> Result<Value, CliError> {
        let mut url = self.run_url(run_id, "/wait");
        if let Some(timeout_seconds) = timeout_seconds {
            url.push_str("&timeout_seconds=");
            url.push_str(&timeout_seconds.to_string());
        }
        let request_timeout = timeout_seconds
            .map(|seconds| Duration::from_secs(seconds.saturating_add(15)))
            .unwrap_or(Duration::from_secs(615));
        let response = self
            .authenticated(ureq::get(&url).timeout(request_timeout))
            .call();
        json_response(response, "wait")
    }

    pub(crate) fn resume(&self, run_id: &str) -> Result<Value, CliError> {
        self.post_control(run_id, "resume")
    }

    pub(crate) fn cancel(&self, run_id: &str) -> Result<Value, CliError> {
        self.post_control(run_id, "cancel")
    }

    pub(crate) fn export(&self, run_id: &str, destination: &Path) -> Result<PathBuf, CliError> {
        let url = self.run_url(run_id, "/export");
        let response = self
            .authenticated(ureq::get(&url).timeout(Duration::from_secs(120)))
            .call();
        let response = successful_response(response, "export")?;
        std::fs::create_dir_all(destination).map_err(|error| io_error("create output", error))?;
        let safe_run_id: String = run_id
            .chars()
            .map(|character| {
                if character.is_ascii_alphanumeric() || matches!(character, '-' | '_') {
                    character
                } else {
                    '_'
                }
            })
            .collect();
        let output_path = destination.join(format!("ask-{safe_run_id}.tar"));
        let mut output = tempfile::NamedTempFile::new_in(destination)
            .map_err(|error| io_error("create export", error))?;
        io::copy(&mut response.into_reader(), &mut output)
            .map_err(|error| io_error("write export", error))?;
        output
            .persist(&output_path)
            .map_err(|error| io_error("publish export", error.error))?;
        Ok(output_path)
    }

    fn post_control(&self, run_id: &str, action: &'static str) -> Result<Value, CliError> {
        let url = self.run_url(run_id, &format!("/{action}"));
        let response = self
            .authenticated(ureq::post(&url).timeout(REQUEST_TIMEOUT))
            .call();
        json_response(response, action)
    }

    fn run_url(&self, run_id: &str, suffix: &str) -> String {
        format!(
            "{}/api/ask/runs/{}{}?project_id={}",
            self.base_url,
            urlencoding::encode(run_id),
            suffix,
            urlencoding::encode(&self.project_id),
        )
    }

    fn authenticated(&self, request: ureq::Request) -> ureq::Request {
        let mut request = apply_bearer_header(request)
            .set(CALLER_PROJECT_HEADER, &self.project_id)
            .set(TARGET_PROJECT_HEADER, &self.project_id);
        for (environment, header) in [
            ("GOBBY_SESSION_ID", SESSION_HEADER),
            ("GOBBY_AGENT_RUN_ID", AGENT_RUN_HEADER),
            ("GOBBY_MANAGED_EXECUTION_ID", MANAGED_EXECUTION_HEADER),
        ] {
            if let Ok(value) = std::env::var(environment) {
                request = request.set(header, &value);
            }
        }
        request
    }
}

fn json_response(
    response: Result<ureq::Response, ureq::Error>,
    operation: &'static str,
) -> Result<Value, CliError> {
    let response = successful_response(response, operation)?;
    response.into_json().map_err(|error| CliError {
        code: "malformed_ask_response",
        message: format!("malformed Ask {operation} response: {error}"),
        recovery: None,
        exit_status: 2,
    })
}

fn successful_response(
    response: Result<ureq::Response, ureq::Error>,
    operation: &'static str,
) -> Result<ureq::Response, CliError> {
    match response {
        Ok(response) => Ok(response),
        Err(ureq::Error::Status(status, response)) => {
            let body = response.into_string().unwrap_or_default();
            let message = response_message(&body);
            let (code, exit_status) = match status {
                400 | 422 => ("invalid_ask_request", 2),
                401 | 403 => ("ask_unauthorized", 2),
                404 => ("ask_run_not_found", 2),
                408 => ("ask_wait_timeout", 2),
                _ => ("ask_daemon_error", 2),
            };
            Err(CliError {
                code,
                message: format!("Ask {operation} failed (HTTP {status}): {message}"),
                recovery: None,
                exit_status,
            })
        }
        Err(ureq::Error::Transport(error)) => Err(CliError {
            code: "ask_daemon_unavailable",
            message: format!("Ask {operation} transport failed: {error}"),
            recovery: Some("start or reconnect to the local Gobby daemon and retry".to_owned()),
            exit_status: 2,
        }),
    }
}

fn response_message(body: &str) -> String {
    serde_json::from_str::<Value>(body)
        .ok()
        .and_then(|value| {
            value
                .get("detail")
                .or_else(|| value.get("message"))
                .and_then(Value::as_str)
                .map(str::to_owned)
        })
        .filter(|message| !message.is_empty())
        .unwrap_or_else(|| "the daemon rejected the request".to_owned())
}

fn io_error(operation: &'static str, error: io::Error) -> CliError {
    CliError {
        code: "ask_export_io",
        message: format!("failed to {operation}: {error}"),
        recovery: None,
        exit_status: 2,
    }
}

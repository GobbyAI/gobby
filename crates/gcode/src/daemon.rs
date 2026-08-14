//! Operator-authenticated daemon clients for global gcode commands.

use std::path::PathBuf;
use std::time::Duration;

use serde::Deserialize;
use serde_json::json;

use crate::cli_error::CliError;
use crate::models::IndexedProject;
use gobby_core::grant::{GrantError, reject_remote_endpoint};
use gobby_core::local_token::apply_bearer_header;

const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LookedUpProject {
    pub id: String,
    pub root: PathBuf,
    pub name: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GlobalPruneOutcome {
    #[serde(default)]
    pub completed: Vec<String>,
    #[serde(default)]
    pub failed: Vec<serde_json::Value>,
    #[serde(default)]
    pub skipped: Vec<serde_json::Value>,
}

#[derive(Debug, Deserialize)]
struct ProjectListing {
    id: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    display_name: Option<String>,
    #[serde(default)]
    repo_path: Option<String>,
    #[serde(default)]
    root_path: Option<String>,
}

impl ProjectListing {
    fn root_path(&self) -> String {
        self.repo_path
            .clone()
            .or_else(|| self.root_path.clone())
            .unwrap_or_default()
    }

    fn matches_name(&self, name: &str) -> bool {
        let root = self.root_path();
        self.name == name
            || self.display_name.as_deref() == Some(name)
            || PathBuf::from(&root)
                .file_name()
                .is_some_and(|file| file == name)
            || root == name
    }
}

pub fn lookup_project_by_name(name: &str) -> Result<LookedUpProject, CliError> {
    let listings = fetch_projects()?;
    listings
        .into_iter()
        .find(|project| project.matches_name(name))
        .and_then(|project| {
            let root = project.root_path();
            if root.is_empty() {
                return None;
            }
            Some(LookedUpProject {
                id: project.id,
                name: project.name,
                root: PathBuf::from(root),
            })
        })
        .ok_or_else(|| CliError {
            code: "project_not_found",
            message: format!("Project '{name}' not found"),
            exit_status: 2,
        })
}

pub fn lookup_project_by_id(project_id: &str) -> Result<LookedUpProject, CliError> {
    let listings = fetch_projects()?;
    listings
        .into_iter()
        .find(|project| project.id == project_id)
        .and_then(|project| {
            let root = project.root_path();
            if root.is_empty() {
                return None;
            }
            Some(LookedUpProject {
                id: project.id,
                name: project.name,
                root: PathBuf::from(root),
            })
        })
        .ok_or_else(|| CliError {
            code: "project_not_found",
            message: format!("Project '{project_id}' not found"),
            exit_status: 2,
        })
}

pub fn list_projects() -> Result<Vec<IndexedProject>, CliError> {
    Ok(fetch_projects()?
        .into_iter()
        .map(|project| IndexedProject {
            id: project.id.clone(),
            root_path: project.root_path(),
            total_files: 0,
            total_symbols: 0,
            last_indexed_at: String::new(),
            index_duration_ms: 0,
            total_eligible_files: None,
        })
        .collect())
}

pub fn post_code_index_prune(
    force: bool,
    retention_days: u32,
) -> Result<GlobalPruneOutcome, CliError> {
    let url = join_daemon_url("/api/code-index/prune")?;
    let request = apply_bearer_header(ureq::post(&url).timeout(REQUEST_TIMEOUT));
    let response = request
        .send_json(json!({
            "force": force,
            "retention_days": retention_days,
        }))
        .map_err(classify_transport)?;
    read_json(response)
}

fn fetch_projects() -> Result<Vec<ProjectListing>, CliError> {
    let url = join_daemon_url("/api/projects")?;
    let request = apply_bearer_header(ureq::get(&url).timeout(REQUEST_TIMEOUT));
    let response = request.call().map_err(classify_transport)?;
    read_json(response)
}

fn join_daemon_url(path: &str) -> Result<String, CliError> {
    let base = gobby_core::daemon_url::daemon_url();
    reject_remote_endpoint(&base).map_err(CliError::grant)?;
    Ok(format!("{}{path}", base.trim_end_matches('/')))
}

fn read_json<T: serde::de::DeserializeOwned>(response: ureq::Response) -> Result<T, CliError> {
    let status = response.status();
    let body = response.into_string().map_err(|error| CliError {
        code: "io",
        message: format!("grant io error: {error}"),
        exit_status: 1,
    })?;
    if !(200..300).contains(&status) {
        return Err(status_error(status, &body));
    }
    serde_json::from_str(&body).map_err(|error| CliError {
        code: "malformed",
        message: format!("malformed grant: {error}"),
        exit_status: 2,
    })
}

fn status_error(status: u16, body: &str) -> CliError {
    match status {
        401 | 403 => CliError::grant(GrantError::DaemonRequired),
        _ => CliError {
            code: "daemon_required",
            message: format!("daemon required ({status}): {body}"),
            exit_status: 2,
        },
    }
}

fn classify_transport(error: ureq::Error) -> CliError {
    match error {
        ureq::Error::Status(status, response) => {
            let body = response.into_string().unwrap_or_default();
            status_error(status, &body)
        }
        ureq::Error::Transport(_) => CliError::grant(GrantError::DaemonRequired),
    }
}

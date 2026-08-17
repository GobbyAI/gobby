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
const PRUNE_TIMEOUT: Duration = Duration::from_secs(30 * 60);

#[derive(Debug, Clone, Copy)]
enum DaemonRequestKind {
    Projects,
    Prune,
}

impl DaemonRequestKind {
    fn noun(self) -> &'static str {
        match self {
            Self::Projects => "project listing",
            Self::Prune => "prune",
        }
    }

    fn error_code(self) -> &'static str {
        match self {
            Self::Projects => "project_request",
            Self::Prune => "prune_request",
        }
    }
}

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
    #[serde(default)]
    total_files: Option<usize>,
    #[serde(default)]
    total_symbols: Option<usize>,
    #[serde(default)]
    last_indexed_at: Option<String>,
    #[serde(default)]
    index_duration_ms: Option<u64>,
    #[serde(default)]
    total_eligible_files: Option<usize>,
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
    unique_project_by_name(fetch_projects()?, name)
}

fn unique_project_by_name(
    listings: Vec<ProjectListing>,
    name: &str,
) -> Result<LookedUpProject, CliError> {
    let matches: Vec<ProjectListing> = listings
        .into_iter()
        .filter(|project| project.matches_name(name))
        .collect();
    match matches.as_slice() {
        [] => Err(CliError {
            code: "project_not_found",
            message: format!("Project '{name}' not found"),
            exit_status: 2,
        }),
        [_] => {
            let project = matches
                .into_iter()
                .next()
                .expect("one name match is present");
            looked_up_project(project).ok_or_else(|| CliError {
                code: "project_not_found",
                message: format!("Project '{name}' not found"),
                exit_status: 2,
            })
        }
        _ => {
            let roots: Vec<String> = matches
                .iter()
                .map(ProjectListing::root_path)
                .filter(|root| !root.is_empty())
                .collect();
            Err(CliError {
                code: "project_ambiguous",
                message: format!(
                    "Project '{name}' matches multiple roots ({}); specify a project path or id",
                    roots.join(", ")
                ),
                exit_status: 2,
            })
        }
    }
}

pub fn lookup_project_by_id(project_id: &str) -> Result<LookedUpProject, CliError> {
    let listings = fetch_projects()?;
    listings
        .into_iter()
        .find(|project| project.id == project_id)
        .and_then(looked_up_project)
        .ok_or_else(|| CliError {
            code: "project_not_found",
            message: format!("Project '{project_id}' not found"),
            exit_status: 2,
        })
}

fn looked_up_project(project: ProjectListing) -> Option<LookedUpProject> {
    let root = project.root_path();
    if root.is_empty() {
        return None;
    }
    Some(LookedUpProject {
        id: project.id,
        name: project.name,
        root: PathBuf::from(root),
    })
}

pub fn list_projects() -> Result<Vec<IndexedProject>, CliError> {
    Ok(fetch_projects()?.into_iter().map(indexed_project).collect())
}

fn indexed_project(project: ProjectListing) -> IndexedProject {
    IndexedProject {
        id: project.id.clone(),
        root_path: project.root_path(),
        total_files: project.total_files.unwrap_or(0),
        total_symbols: project.total_symbols.unwrap_or(0),
        last_indexed_at: project.last_indexed_at.unwrap_or_default(),
        index_duration_ms: project.index_duration_ms.unwrap_or(0),
        total_eligible_files: project.total_eligible_files,
    }
}

pub fn post_code_index_prune(
    force: bool,
    retention_days: u32,
) -> Result<GlobalPruneOutcome, CliError> {
    let url = join_daemon_url("/api/code-index/prune")?;
    let request = apply_bearer_header(ureq::post(&url).timeout(PRUNE_TIMEOUT));
    let response = request
        .send_json(json!({
            "force": force,
            "retention_days": retention_days,
        }))
        .map_err(|error| classify_transport(error, DaemonRequestKind::Prune))?;
    read_json(response, DaemonRequestKind::Prune)
}

fn fetch_projects() -> Result<Vec<ProjectListing>, CliError> {
    let url = join_daemon_url("/api/projects")?;
    let request = apply_bearer_header(ureq::get(&url).timeout(REQUEST_TIMEOUT));
    let response = request
        .call()
        .map_err(|error| classify_transport(error, DaemonRequestKind::Projects))?;
    read_json(response, DaemonRequestKind::Projects)
}

fn join_daemon_url(path: &str) -> Result<String, CliError> {
    let base = gobby_core::daemon_url::daemon_url();
    reject_remote_endpoint(&base).map_err(CliError::grant)?;
    Ok(format!("{}{path}", base.trim_end_matches('/')))
}

fn read_json<T: serde::de::DeserializeOwned>(
    response: ureq::Response,
    kind: DaemonRequestKind,
) -> Result<T, CliError> {
    let status = response.status();
    let body = response.into_string().map_err(|error| CliError {
        code: "io",
        message: format!("{} io error: {error}", kind.noun()),
        exit_status: 1,
    })?;
    if !(200..300).contains(&status) {
        return Err(status_error(status, &body, kind));
    }
    serde_json::from_str(&body).map_err(|error| CliError {
        code: "malformed",
        message: format!("malformed {}: {error}", kind.noun()),
        exit_status: 2,
    })
}

fn status_error(status: u16, body: &str, kind: DaemonRequestKind) -> CliError {
    match status {
        401 | 403 => CliError::grant(GrantError::DaemonRequired),
        _ => CliError {
            code: kind.error_code(),
            message: format!("{} failed ({status}): {}", kind.noun(), sanitize_body(body)),
            exit_status: 2,
        },
    }
}

fn sanitize_body(body: &str) -> String {
    let collapsed = body.split_whitespace().collect::<Vec<_>>().join(" ");
    const MAX: usize = 120;
    let mut chars = collapsed.chars();
    let truncated: String = chars.by_ref().take(MAX).collect();
    if chars.next().is_some() {
        format!("{truncated}…")
    } else {
        truncated
    }
}

fn classify_transport(error: ureq::Error, kind: DaemonRequestKind) -> CliError {
    match error {
        ureq::Error::Status(status, response) => {
            let body = response.into_string().unwrap_or_default();
            status_error(status, &body, kind)
        }
        ureq::Error::Transport(transport) if is_timeout(&transport) => {
            CliError::grant(GrantError::Timeout)
        }
        ureq::Error::Transport(_) => CliError::grant(GrantError::DaemonRequired),
    }
}

fn is_timeout(transport: &ureq::Transport) -> bool {
    let mut source = std::error::Error::source(transport);
    while let Some(err) = source {
        if let Some(io) = err.downcast_ref::<std::io::Error>()
            && io.kind() == std::io::ErrorKind::TimedOut
        {
            return true;
        }
        source = err.source();
    }
    let rendered = transport.to_string().to_ascii_lowercase();
    rendered.contains("timed out") || rendered.contains("timeout")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn listing(id: &str, name: &str, root: &str) -> ProjectListing {
        ProjectListing {
            id: id.to_string(),
            name: name.to_string(),
            display_name: None,
            repo_path: Some(root.to_string()),
            root_path: None,
            total_files: None,
            total_symbols: None,
            last_indexed_at: None,
            index_duration_ms: None,
            total_eligible_files: None,
        }
    }

    #[test]
    fn lookup_project_by_name_errors_on_two_matches() {
        let error = unique_project_by_name(
            vec![
                listing("one", "gobby", "/tmp/a/gobby"),
                listing("two", "gobby", "/tmp/b/gobby"),
            ],
            "gobby",
        )
        .expect_err("ambiguous name");
        assert_eq!(error.code, "project_ambiguous");
        assert!(error.message.contains("/tmp/a/gobby"), "{}", error.message);
        assert!(error.message.contains("/tmp/b/gobby"), "{}", error.message);
        assert!(
            error.message.contains("specify a project path or id"),
            "{}",
            error.message
        );
    }

    #[test]
    fn lookup_project_by_name_returns_the_unique_match() {
        let found = unique_project_by_name(
            vec![
                listing("one", "gobby", "/tmp/a/gobby"),
                listing("two", "other", "/tmp/other"),
            ],
            "gobby",
        )
        .expect("unique name");
        assert_eq!(found.id, "one");
        assert_eq!(found.root, PathBuf::from("/tmp/a/gobby"));
    }

    #[test]
    fn status_error_keeps_auth_mapping_and_sanitizes_other_bodies() {
        let auth = status_error(401, "secret-token", DaemonRequestKind::Projects);
        assert_eq!(auth.code, "daemon_required");
        let prune = status_error(
            500,
            &format!("{}\n{}", "x".repeat(200), "y".repeat(200)),
            DaemonRequestKind::Prune,
        );
        assert_eq!(prune.code, "prune_request");
        assert!(prune.message.starts_with("prune failed (500):"));
        assert!(!prune.message.contains('\n'));
        assert!(prune.message.ends_with('…'));
        assert!(prune.message.len() < 180);
    }

    #[test]
    fn indexed_project_json_omits_zeroed_index_fields() {
        let payload = serde_json::to_value(&IndexedProject {
            id: "proj".to_string(),
            root_path: "/tmp/proj".to_string(),
            total_files: 0,
            total_symbols: 0,
            last_indexed_at: String::new(),
            index_duration_ms: 0,
            total_eligible_files: None,
        })
        .expect("json");
        assert_eq!(payload["id"], "proj");
        assert_eq!(payload["root_path"], "/tmp/proj");
        assert!(payload.get("total_files").is_none());
        assert!(payload.get("total_symbols").is_none());
        assert!(payload.get("last_indexed_at").is_none());
        assert!(payload.get("index_duration_ms").is_none());
    }

    #[test]
    fn indexed_project_maps_missing_listing_stats_to_defaults() {
        let mapped = indexed_project(listing("proj", "proj", "/tmp/proj"));
        assert_eq!(mapped.id, "proj");
        assert_eq!(mapped.root_path, "/tmp/proj");
        assert_eq!(mapped.total_files, 0);
        assert_eq!(mapped.total_symbols, 0);
        assert_eq!(mapped.last_indexed_at, "");
        assert_eq!(mapped.index_duration_ms, 0);
        assert_eq!(mapped.total_eligible_files, None);
    }
}

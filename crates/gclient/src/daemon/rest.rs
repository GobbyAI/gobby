use super::{
    Answer, DaemonError, Page, ProjectRow, RosterEntry, RunRow, SessionRow, SourceStatus,
    TerminalRow, WorktreeRow, REQUEST_DEADLINE,
};
use reqwest::{Client, Method, Response, StatusCode, Url};
use serde::de::DeserializeOwned;
use serde::Deserialize;
use serde_json::Value;
use std::time::Duration;
use tokio::time::timeout;

#[derive(Debug, Clone)]
pub(super) struct RestClient {
    client: Client,
    base_url: Url,
    token: String,
}

impl RestClient {
    pub(super) fn new(base_url: Url, token: String) -> Self {
        Self {
            client: Client::new(),
            base_url,
            token,
        }
    }

    pub(super) async fn list_terminals(
        &self,
        project: &str,
        cursor: Option<&str>,
    ) -> Result<Page<TerminalRow>, DaemonError> {
        let mut url = self.url(&["api", "terminals"])?;
        {
            let mut query = url.query_pairs_mut();
            query.append_pair("project_id", project);
            query.append_pair("states", "pending,live");
            if let Some(cursor) = cursor {
                query.append_pair("cursor", cursor);
            }
        }
        self.json(Method::GET, url, None).await
    }

    pub(super) async fn terminal(&self, terminal_id: &str) -> Result<TerminalRow, DaemonError> {
        let url = self.url(&["api", "terminals", terminal_id])?;
        self.json(Method::GET, url, None).await
    }

    pub(super) async fn roster(&self) -> Result<Vec<RosterEntry>, DaemonError> {
        Ok(self.roster_snapshot().await?.entries)
    }

    pub(super) async fn roster_snapshot(&self) -> Result<AttentionRoster, DaemonError> {
        let url = self.url(&["api", "attention", "roster"])?;
        self.json(Method::GET, url, None).await
    }

    pub(super) async fn projects(&self) -> Result<Vec<ProjectRow>, DaemonError> {
        let url = self.url(&["api", "projects"])?;
        self.json(Method::GET, url, None).await
    }

    pub(super) async fn source_status(&self, project: &str) -> Result<SourceStatus, DaemonError> {
        let mut url = self.url(&["api", "source-control", "status"])?;
        url.query_pairs_mut().append_pair("project_id", project);
        self.json(Method::GET, url, None).await
    }

    pub(super) async fn worktrees(&self, project: &str) -> Result<Vec<WorktreeRow>, DaemonError> {
        let mut url = self.url(&["api", "source-control", "worktrees"])?;
        url.query_pairs_mut().append_pair("project_id", project);
        let envelope: Worktrees = self.json(Method::GET, url, None).await?;
        Ok(envelope.worktrees)
    }

    /// Live sessions only: the sidebar shows what is running, and the route
    /// caps a page at 1000, which covers a project's live set many times over.
    pub(super) async fn sessions(&self, project: &str) -> Result<Vec<SessionRow>, DaemonError> {
        let mut url = self.url(&["api", "sessions"])?;
        {
            let mut query = url.query_pairs_mut();
            query.append_pair("project_id", project);
            query.append_pair("limit", "1000");
            for status in LIVE_SESSION_STATUSES {
                query.append_pair("status_in", status);
            }
        }
        let envelope: Sessions = self.json(Method::GET, url, None).await?;
        Ok(envelope.sessions)
    }

    pub(super) async fn agent_runs(&self, project: &str) -> Result<Vec<RunRow>, DaemonError> {
        let mut url = self.url(&["api", "agents", "runs"])?;
        {
            let mut query = url.query_pairs_mut();
            query.append_pair("project_id", project);
            query.append_pair("limit", "200");
        }
        let envelope: Runs = self.json(Method::GET, url, None).await?;
        Ok(envelope.runs)
    }

    pub(super) async fn respond(
        &self,
        entry: &str,
        attention_id: &str,
        answer: &Answer,
    ) -> Result<(), DaemonError> {
        let url = self.url(&["api", "attention", entry, "respond"])?;
        self.empty(Method::POST, url, Some(answer.response_body(attention_id)))
            .await
    }

    pub(super) async fn mark_seen(
        &self,
        entry: &str,
        attention_id: &str,
    ) -> Result<(), DaemonError> {
        let url = self.url(&["api", "attention", entry, "seen"])?;
        self.empty(
            Method::POST,
            url,
            Some(serde_json::json!({"attention_id": attention_id})),
        )
        .await
    }

    fn url(&self, segments: &[&str]) -> Result<Url, DaemonError> {
        let mut url = self.base_url.clone();
        url.set_query(None);
        url.set_fragment(None);
        url.path_segments_mut()
            .map_err(|()| DaemonError::Protocol {
                detail: "daemon URL cannot be a base URL".into(),
            })?
            .clear()
            .extend(segments.iter().copied());
        Ok(url)
    }

    async fn send(
        &self,
        method: Method,
        url: Url,
        body: Option<Value>,
    ) -> Result<Response, DaemonError> {
        let mut request = self.client.request(method, url).bearer_auth(&self.token);
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = timeout(REQUEST_DEADLINE, request.send())
            .await
            .map_err(|_| DaemonError::Timeout)?
            .map_err(|_| DaemonError::Unavailable { retry_after: None })?;
        if response.status().is_success() {
            return Ok(response);
        }
        Err(status_error(response).await)
    }

    async fn json<T: DeserializeOwned>(
        &self,
        method: Method,
        url: Url,
        body: Option<Value>,
    ) -> Result<T, DaemonError> {
        let response = self.send(method, url, body).await?;
        timeout(REQUEST_DEADLINE, response.json())
            .await
            .map_err(|_| DaemonError::Timeout)?
            .map_err(protocol)
    }

    async fn empty(
        &self,
        method: Method,
        url: Url,
        body: Option<Value>,
    ) -> Result<(), DaemonError> {
        self.send(method, url, body).await?;
        Ok(())
    }
}

/// `LIVE_SESSION_STATUS_ORDER` in the daemon's session constants.
const LIVE_SESSION_STATUSES: [&str; 6] = [
    "active",
    "paused",
    "interrupted",
    "awaiting_input",
    "awaiting_approval",
    "awaiting_handoff",
];

#[derive(Debug, Deserialize)]
struct Worktrees {
    #[serde(default)]
    worktrees: Vec<WorktreeRow>,
}

#[derive(Debug, Deserialize)]
struct Sessions {
    #[serde(default)]
    sessions: Vec<SessionRow>,
}

#[derive(Debug, Deserialize)]
struct Runs {
    #[serde(default)]
    runs: Vec<RunRow>,
}

#[derive(Debug, Deserialize)]
pub(super) struct AttentionRoster {
    pub(super) epoch: String,
    #[serde(deserialize_with = "super::ws::deserialize_safe_u64")]
    pub(super) seq: u64,
    pub(super) entries: Vec<RosterEntry>,
}

fn protocol(error: impl std::fmt::Display) -> DaemonError {
    DaemonError::Protocol {
        detail: error.to_string(),
    }
}

async fn status_error(response: Response) -> DaemonError {
    let status = response.status();
    if matches!(status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN) {
        return DaemonError::Unauthorized;
    }
    if status == StatusCode::NOT_FOUND {
        return DaemonError::NotFound;
    }
    if status.is_server_error() || status == StatusCode::TOO_MANY_REQUESTS {
        let retry_after = response
            .headers()
            .get(reqwest::header::RETRY_AFTER)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.parse::<u64>().ok())
            .map(Duration::from_secs);
        return DaemonError::Unavailable { retry_after };
    }
    let detail = timeout(REQUEST_DEADLINE, response.text())
        .await
        .ok()
        .and_then(Result::ok)
        .filter(|body| !body.is_empty())
        .unwrap_or_else(|| format!("daemon returned HTTP {status}"));
    DaemonError::Protocol { detail }
}

use super::{Answer, DaemonError, Page, RosterEntry, TerminalRow, REQUEST_DEADLINE};
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

//! The v2 request sender of the full-daemon harness: one corpus
//! case over blocking HTTP, and the response-only classifier. The client never reads proxy
//! settings from the environment, so a local proxy setting cannot reroute a replay.

use std::fmt;
use std::io::Read;
use std::time::Duration;

use serde_json::{Map, Value, json};

use crate::corpus_v2::{RESPONSE_HEADER_ALLOWLIST, is_json, normalized_response, request_target};

/// Per-request timeout, the same ten seconds the gdaemon replay uses.
pub const SEND_TIMEOUT: Duration = Duration::from_secs(10);

/// The credentials a case's `credential` names: `none`, `operator` or `grant`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Credentials {
    /// The bootstrap API key, sent as `Authorization: Bearer <key>` for `operator` cases.
    pub operator_key: Option<String>,
    /// The grant presentation headers for `grant` cases, as (name, value) pairs.
    pub grant_headers: Vec<(String, String)>,
}

/// A replay step that could not complete.
#[derive(Debug)]
pub enum SendError {
    /// The case names a credential the caller did not supply.
    MissingCredential(String),
    /// The case file is malformed.
    Case(String),
    /// The request did not complete: connect, timeout or I/O.
    Transport(String),
    /// The response could not be captured as the corpus expects.
    Response(String),
}

impl fmt::Display for SendError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingCredential(detail) => write!(f, "missing credential: {detail}"),
            Self::Case(detail) => write!(f, "malformed case: {detail}"),
            Self::Transport(detail) => write!(f, "transport: {detail}"),
            Self::Response(detail) => write!(f, "response: {detail}"),
        }
    }
}

impl std::error::Error for SendError {}

/// The blocking client for the replay. Proxy settings from the environment are ignored,
/// redirects are not followed, so a 3xx is captured as the daemon sent it.
pub fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .try_proxy_from_env(false)
        .redirects(0)
        .timeout(SEND_TIMEOUT)
        .build()
}

/// Send one case's request to `front_door` (for example `http://127.0.0.1:8080`) and return the
/// captured document `{"response": {"status", "headers", "body"}}`, before normalization.
pub fn send_case(
    agent: &ureq::Agent,
    front_door: &str,
    case: &Value,
    credentials: &Credentials,
) -> Result<Value, SendError> {
    let name = case_name(case);
    let request = &case["request"];
    let method = request["method"]
        .as_str()
        .ok_or_else(|| SendError::Case(format!("{name}: request method")))?;
    let url = format!("{front_door}{}", request_target(request));
    let headers_object = request["headers"]
        .as_object()
        .ok_or_else(|| SendError::Case(format!("{name}: request headers")))?;
    let body = match &request["body"] {
        Value::Null => None,
        body => Some(
            serde_json::to_vec(body)
                .map_err(|error| SendError::Case(format!("{name}: body: {error}")))?,
        ),
    };
    // The order of tests/contracts/http_corpus.py:send_case: the credential's headers first, then
    // the case's own headers, which replace a credential header of the same name.
    let mut headers = vec![("host".to_owned(), "localhost".to_owned())];
    if body.is_some() {
        merge_header(&mut headers, "content-type", "application/json");
    }
    let credential = case["credential"].as_str().unwrap_or("none");
    for (header, value) in credential_headers(&name, credential, credentials)? {
        merge_header(&mut headers, &header, &value);
    }
    for (header, value) in headers_object {
        let value = value
            .as_str()
            .ok_or_else(|| SendError::Case(format!("{name}: header {header:?} value")))?;
        merge_header(&mut headers, header, value);
    }
    let mut outgoing = agent.request(method, &url);
    for (header, value) in &headers {
        outgoing = outgoing.set(header, value);
    }
    let result = match body {
        None => outgoing.call(),
        Some(bytes) => outgoing.send_bytes(&bytes),
    };
    let response = match result {
        Ok(response) => response,
        Err(ureq::Error::Status(_, response)) => response,
        Err(ureq::Error::Transport(error)) => {
            return Err(SendError::Transport(format!("{name}: {error}")));
        }
    };
    capture(&name, response)
}

/// The response-only comparison: the captured response and the case's recorded response, after
/// the same redaction and masks, must be equal. Database deltas and side effects are compared by
/// the replay driver, not here.
pub fn classify_response(case: &Value, captured: Value) -> Result<(), String> {
    let name = case_name(case);
    let expected = normalized_response(case.clone(), &case["mask"]);
    let actual = normalized_response(captured, &case["mask"]);
    if actual == expected {
        Ok(())
    } else {
        Err(format!(
            "{name}: response differs\nexpected: {expected}\nactual:   {actual}"
        ))
    }
}

fn case_name(case: &Value) -> String {
    case["name"].as_str().unwrap_or("<unnamed>").to_owned()
}

/// The headers a `credential` recipe names (tests/contracts/http_corpus.py:credential_headers).
fn credential_headers(
    name: &str,
    credential: &str,
    credentials: &Credentials,
) -> Result<Vec<(String, String)>, SendError> {
    match credential {
        "none" => Ok(Vec::new()),
        "operator" => credentials
            .operator_key
            .as_ref()
            .map(|key| vec![("Authorization".to_owned(), format!("Bearer {key}"))])
            .ok_or_else(|| SendError::MissingCredential(format!("{name}: operator key"))),
        "grant" if credentials.grant_headers.is_empty() => Err(SendError::MissingCredential(
            format!("{name}: grant headers"),
        )),
        "grant" => Ok(credentials.grant_headers.clone()),
        other => Err(SendError::Case(format!(
            "{name}: unknown credential {other:?}"
        ))),
    }
}

/// Set `name` to `value`, replacing any header of that name whatever its case. ureq's `set`
/// replaces only an exact-case match for names that do not start with `x-`, and appends for those
/// that do, so the list keeps one entry per name before anything reaches ureq.
fn merge_header(headers: &mut Vec<(String, String)>, name: &str, value: &str) {
    headers.retain(|(existing, _)| !existing.eq_ignore_ascii_case(name));
    headers.push((name.to_owned(), value.to_owned()));
}

/// Capture a response the way the replay does: allowlisted headers, then the body as JSON when
/// the content type says so, as text otherwise, and null when empty.
fn capture(name: &str, response: ureq::Response) -> Result<Value, SendError> {
    let status = response.status();
    let content_type = response.header("content-type").map(str::to_owned);
    let headers: Map<String, Value> = RESPONSE_HEADER_ALLOWLIST
        .iter()
        .filter_map(|header| {
            response
                .header(header)
                .map(|value| ((*header).to_owned(), Value::String(value.to_owned())))
        })
        .collect();
    let mut raw = Vec::new();
    response
        .into_reader()
        .read_to_end(&mut raw)
        .map_err(|error| SendError::Transport(format!("{name}: reading the body: {error}")))?;
    let body = if raw.is_empty() {
        Value::Null
    } else if is_json(content_type.as_deref()) {
        serde_json::from_slice(&raw)
            .map_err(|error| SendError::Response(format!("{name}: json body: {error}")))?
    } else {
        Value::String(
            String::from_utf8(raw)
                .map_err(|error| SendError::Response(format!("{name}: utf-8 body: {error}")))?,
        )
    };
    Ok(json!({"response": {"status": status, "headers": headers, "body": body}}))
}

/// The status of a `GET` to `url`, with no credential. A non-2xx status is returned, not an error.
pub fn get_status(url: &str) -> Result<u16, SendError> {
    match agent().get(url).call() {
        Ok(response) => Ok(response.status()),
        Err(ureq::Error::Status(status, _)) => Ok(status),
        Err(ureq::Error::Transport(error)) => Err(SendError::Transport(format!("{url}: {error}"))),
    }
}

/// A JSON `POST` to `url`, with an optional operator bearer. Returns the status and the parsed
/// body, whatever the status is.
pub fn post_json(url: &str, bearer: Option<&str>, body: &Value) -> Result<(u16, Value), SendError> {
    let mut outgoing = agent().post(url).set("content-type", "application/json");
    if let Some(key) = bearer {
        outgoing = outgoing.set("Authorization", &format!("Bearer {key}"));
    }
    let result = outgoing.send_bytes(
        &serde_json::to_vec(body)
            .map_err(|error| SendError::Case(format!("{url}: body: {error}")))?,
    );
    let response = match result {
        Ok(response) => response,
        Err(ureq::Error::Status(_, response)) => response,
        Err(ureq::Error::Transport(error)) => {
            return Err(SendError::Transport(format!("{url}: {error}")));
        }
    };
    let status = response.status();
    let captured = capture(url, response)?;
    Ok((status, captured["response"]["body"].clone()))
}

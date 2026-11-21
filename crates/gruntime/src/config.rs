//! `GET /api/config/effective` over Python's published active configuration.

use std::collections::BTreeMap;
use std::error::Error;
use std::sync::Arc;

use axum::body::Body;
use axum::extract::State;
use axum::http::header::{CACHE_CONTROL, CONTENT_TYPE};
use axum::http::{HeaderValue, Response, StatusCode};
use gobby_core::config::{is_machine_config_key, is_secret_reference_key};
use gobby_core::grant::deployment_token;
use gobby_core::json::{Json, PYTHON, dumps};
use gobby_core::postgres_pool::Pool;
use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::RuntimeState;

/// The published active state of one deployment.
pub struct ActiveState {
    pub revision: i64,
    pub document: StateDocument,
}

/// Python's `config_active_state.state` document. Unknown keys are ignored.
#[derive(Deserialize)]
pub struct StateDocument {
    pub desired_values: Map<String, Value>,
    pub active_values: Map<String, Value>,
    pub desired_overrides: Map<String, Value>,
    pub pending_restart_keys: Vec<String>,
    pub failed_live_keys: BTreeMap<String, FailedLiveKey>,
    pub desired_bindings: BTreeMap<String, SealedBinding>,
    pub active_bindings: BTreeMap<String, SealedBinding>,
}

#[derive(Deserialize)]
pub struct FailedLiveKey {
    pub revision: i64,
    pub subscriber: String,
}

/// A secret binding, kept sealed until a caller opens it.
#[derive(Deserialize)]
pub struct SealedBinding {
    pub reference: String,
    pub fingerprint: String,
    pub sealed: Option<String>,
}

/// The deployment's published state, or `None` before Python has published it.
pub async fn active_state(
    pool: &Pool,
    token: &str,
) -> Result<Option<ActiveState>, Box<dyn Error + Send + Sync>> {
    let client = pool.get().await?;
    let Some(row) = client
        .query_opt(
            "SELECT revision, state::text FROM config_active_state WHERE deployment_token = $1",
            &[&token],
        )
        .await?
    else {
        return Ok(None);
    };
    let revision: i64 = row.try_get(0)?;
    let state: String = row.try_get(1)?;
    Ok(Some(ActiveState {
        revision,
        document: serde_json::from_str(&state)?,
    }))
}

/// The machine-scoped active values `/api/config/effective` reports, expanded.
pub fn machine_values(active: &ActiveState) -> BTreeMap<String, String> {
    active
        .document
        .active_values
        .iter()
        .filter(|(key, value)| {
            is_machine_config_key(key) && !is_secret_reference_key(key) && !value.is_null()
        })
        .filter_map(|(key, value)| {
            let expanded = expand_env(&stringify(value));
            (!is_unresolved(&expanded)).then(|| (key.clone(), expanded))
        })
        .collect()
}

fn stringify(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        // Python's `_stringify_config_value` returns `json.dumps(value)`.
        other => {
            dumps(&Json::from(other), PYTHON).expect("PYTHON writes every float a Value can hold")
        }
    }
}

/// Python's `expand_env_vars` over `${NAME}` and `${NAME:-default}`. A token
/// whose name is not a valid identifier stays as written.
fn expand_env(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find("${") {
        out.push_str(&rest[..start]);
        let token = &rest[start..];
        let Some(close) = token.find('}') else {
            out.push_str(token);
            return out;
        };
        if let Some((name, default)) = parse_reference(&token[2..close]) {
            out.push_str(&resolve(name, default).unwrap_or_else(|| token[..=close].to_owned()));
            rest = &token[close + 1..];
        } else {
            out.push('$');
            rest = &token[1..];
        }
    }
    out.push_str(rest);
    out
}

/// Splits the inside of `${...}` into a valid name and its optional default.
fn parse_reference(inner: &str) -> Option<(&str, Option<&str>)> {
    let (name, default) = match inner.split_once(":-") {
        Some((name, default)) => (name, Some(default)),
        None => (inner, None),
    };
    let mut chars = name.chars();
    let valid = chars
        .next()
        .is_some_and(|first| first == '_' || first.is_ascii_alphabetic())
        && chars.all(|c| c == '_' || c.is_ascii_alphanumeric());
    valid.then_some((name, default))
}

/// The environment value when set and non-empty, else the default when one is
/// written (even empty), else `None` so the token stays unchanged.
fn resolve(name: &str, default: Option<&str>) -> Option<String> {
    match std::env::var(name) {
        Ok(value) if !value.is_empty() => Some(value),
        _ => default.map(str::to_owned),
    }
}

/// Whether an expanded value still holds a secret reference or an unresolved
/// `${...}` token.
fn is_unresolved(value: &str) -> bool {
    value.contains("$secret:")
        || value.match_indices("${").any(|(index, _)| {
            value[index + 2..]
                .find(['{', '}'])
                .is_some_and(|close| value.as_bytes()[index + 2 + close] == b'}')
        })
}

pub(crate) async fn effective(State(state): State<Arc<RuntimeState>>) -> Response<Body> {
    let token = deployment_token(&state.home);
    match active_state(&state.pool, &token).await {
        Ok(Some(active)) => {
            let body = json!({"revision": active.revision, "config": machine_values(&active)});
            let mut response = json_response(StatusCode::OK, body);
            response
                .headers_mut()
                .insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
            response
        }
        Ok(None) => json_response(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({
                "error": {
                    "code": "runtime_unavailable",
                    "message": "Configuration runtime is still starting",
                    "path": [],
                    "retryable": true,
                },
            }),
        ),
        Err(error) => {
            eprintln!("gdaemon runtime: config active state read failed: {error}");
            json_response(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({"detail": "Internal server error"}),
            )
        }
    }
}

pub(crate) fn json_response(status: StatusCode, body: Value) -> Response<Body> {
    let mut response = Response::new(Body::from(body.to_string()));
    *response.status_mut() = status;
    response
        .headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    response
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::stringify;

    #[test]
    fn non_string_values_format_like_python_json_dumps() {
        assert_eq!(
            stringify(&json!(["é", 1e-5, 1e16, 2.5])),
            "[\"\x5cu00e9\", 1e-05, 1e+16, 2.5]"
        );
        assert_eq!(
            stringify(&json!({"k": true, "n": 3})),
            r#"{"k": true, "n": 3}"#
        );
    }
}

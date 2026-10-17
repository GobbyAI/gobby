//! `GET /api/config/effective` over Python's published active configuration.

use std::collections::BTreeMap;
use std::error::Error;
use std::io::{self, Write};
use std::sync::Arc;

use axum::body::Body;
use axum::extract::State;
use axum::http::header::{CACHE_CONTROL, CONTENT_TYPE};
use axum::http::{HeaderValue, Response, StatusCode};
use gobby_core::config::{is_machine_config_key, is_secret_reference_key};
use gobby_core::grant::deployment_token;
use gobby_core::postgres_pool::Pool;
use serde::{Deserialize, Serialize};
use serde_json::ser::Formatter;
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
        other => {
            let mut out = Vec::new();
            other
                .serialize(&mut serde_json::Serializer::with_formatter(
                    &mut out, PythonJson,
                ))
                .expect("a JSON value serializes into a Vec");
            String::from_utf8(out).expect("serde_json writes UTF-8")
        }
    }
}

/// Python's `json.dumps` defaults: `", "` and `": "` separators, ASCII-only
/// strings, and `float.__repr__` for floats.
struct PythonJson;

impl Formatter for PythonJson {
    fn begin_array_value<W: ?Sized + Write>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> io::Result<()> {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }

    fn begin_object_key<W: ?Sized + Write>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> io::Result<()> {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }

    fn begin_object_value<W: ?Sized + Write>(&mut self, writer: &mut W) -> io::Result<()> {
        writer.write_all(b": ")
    }

    fn write_f64<W: ?Sized + Write>(&mut self, writer: &mut W, value: f64) -> io::Result<()> {
        writer.write_all(python_float_repr(value).as_bytes())
    }

    fn write_string_fragment<W: ?Sized + Write>(
        &mut self,
        writer: &mut W,
        fragment: &str,
    ) -> io::Result<()> {
        for c in fragment.chars() {
            if (' '..='~').contains(&c) {
                writer.write_all(c.encode_utf8(&mut [0; 4]).as_bytes())?;
            } else {
                for unit in c.encode_utf16(&mut [0; 2]) {
                    write!(writer, "\\u{unit:04x}")?;
                }
            }
        }
        Ok(())
    }
}

/// Python's `repr(float)`: the shortest round-trip digits, exponent form below
/// `1e-4` and from `1e16`, and a trailing `.0` on integral fixed-form values.
fn python_float_repr(value: f64) -> String {
    let sign = if value.is_sign_negative() { "-" } else { "" };
    let magnitude = value.abs();
    if magnitude == 0.0 {
        return format!("{sign}0.0");
    }
    // `{:e}` prints the shortest round-trip digits as `d[.ddd]e<exponent>`.
    let scientific = format!("{magnitude:e}");
    let (mantissa, exponent) = scientific
        .split_once('e')
        .expect("`{:e}` output has an exponent");
    let exponent: i32 = exponent.parse().expect("`{:e}` exponent is an integer");
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    if (-4..16).contains(&exponent) {
        let body = if exponent < 0 {
            format!("0.{}{digits}", "0".repeat((-exponent - 1) as usize))
        } else {
            let int_len = exponent as usize + 1;
            if digits.len() <= int_len {
                format!("{digits}{}.0", "0".repeat(int_len - digits.len()))
            } else {
                format!("{}.{}", &digits[..int_len], &digits[int_len..])
            }
        };
        format!("{sign}{body}")
    } else {
        let (head, tail) = digits.split_at(1);
        let fraction = if tail.is_empty() {
            String::new()
        } else {
            format!(".{tail}")
        };
        let exp_sign = if exponent < 0 { '-' } else { '+' };
        format!("{sign}{head}{fraction}e{exp_sign}{:02}", exponent.abs())
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

fn json_response(status: StatusCode, body: Value) -> Response<Body> {
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

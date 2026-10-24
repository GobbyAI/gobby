//! Native port of `capabilities_from_snapshot` (`src/gobby/runtime_grants/service.py`).
//! It reads the published flat values and the opened active secret bindings, and
//! decides each capability the same way Python does.
//!
//! Published active state carries every registry key, so each read is a plain lookup.
//! The leaf issues `PostgresDirect` only; the Falkor and Qdrant broker operations are
//! ported as they are today until #24712 removes them.

use std::collections::BTreeMap;

use gobby_core::grant::{
    AiCapability, BrokerOperation, FalkorCapability, GrantCapabilities, PostgresCapability,
    QdrantCapability,
};
use serde_json::{Map, Value};
use url::Url;

const FALKOR_HOST: &str = "databases.falkordb.host";
const FALKOR_PORT: &str = "databases.falkordb.port";
const FALKOR_PASSWORD: &str = "databases.falkordb.password";
const QDRANT_URL: &str = "databases.qdrant.url";
const QDRANT_API_KEY: &str = "databases.qdrant.api_key";
const EMBED_MODEL: &str = "ai.embeddings.model";

pub fn capabilities_from_snapshot(
    values: &Map<String, Value>,
    secrets: &BTreeMap<String, Option<String>>,
    postgres: PostgresCapability,
) -> GrantCapabilities {
    let falkordb = falkor(values, secrets);
    let qdrant = qdrant(values, secrets);
    let embed_enabled =
        text(values, EMBED_MODEL).is_some_and(|model| !model.is_empty() && !unresolved(model));
    let mut broker_operations = Vec::new();
    if embed_enabled {
        broker_operations.push(operation("embed", "POST", "/api/embeddings"));
    }
    if let FalkorCapability::Brokered { operations } = &falkordb {
        broker_operations.extend(operations.iter().cloned());
    }
    if let QdrantCapability::Brokered { operations } = &qdrant {
        broker_operations.extend(operations.iter().cloned());
    }
    GrantCapabilities {
        postgres,
        falkordb,
        qdrant,
        embed: ai(embed_enabled),
        text_generate: AiCapability::Daemon {},
        tool_chat: AiCapability::Daemon {},
        vision_extract: ai(vision_extract_enabled(values)),
        audio_transcribe: ai(audio_transcribe_enabled(values)),
        broker_operations,
    }
}

fn falkor(
    values: &Map<String, Value>,
    secrets: &BTreeMap<String, Option<String>>,
) -> FalkorCapability {
    let config_password = text(values, FALKOR_PASSWORD);
    let password = resolved_secret(secrets, FALKOR_PASSWORD)
        .or_else(|| plain(config_password).map(str::to_owned));
    let host = match text(values, FALKOR_HOST) {
        Some(host) if !host.is_empty() && !unresolved(host) => host,
        _ => return FalkorCapability::Unavailable {},
    };
    let Some(port) = values.get(FALKOR_PORT).and_then(Value::as_i64) else {
        return FalkorCapability::Unavailable {};
    };
    let password_unresolved = match &password {
        Some(password) => unresolved(password),
        None => config_password.is_some_and(unresolved),
    };
    if password_unresolved {
        return FalkorCapability::Brokered {
            operations: falkor_operations(),
        };
    }
    let password = password.unwrap_or_default();
    if password.is_empty() && !is_loopback(host) {
        return FalkorCapability::Brokered {
            operations: falkor_operations(),
        };
    }
    FalkorCapability::Direct {
        host: host.to_owned(),
        port,
        password,
    }
}

fn qdrant(
    values: &Map<String, Value>,
    secrets: &BTreeMap<String, Option<String>>,
) -> QdrantCapability {
    let url = text(values, QDRANT_URL);
    let api_key = resolved_secret(secrets, QDRANT_API_KEY)
        .or_else(|| plain(text(values, QDRANT_API_KEY)).map(str::to_owned))
        .unwrap_or_default();
    if url_requires_broker(url) {
        return match url {
            Some(url) if !url.is_empty() => QdrantCapability::Brokered {
                operations: vec![operation(
                    "invalidate_projection",
                    "POST",
                    "/api/code-index/invalidate",
                )],
            },
            _ => QdrantCapability::Unavailable {},
        };
    }
    let Some(url) = url.filter(|url| !url.is_empty()) else {
        return QdrantCapability::Unavailable {};
    };
    let hostname = Url::parse(url)
        .ok()
        .and_then(|parsed| parsed.host_str().map(str::to_owned))
        .unwrap_or_default();
    if api_key.is_empty() && !is_loopback(&hostname) {
        return QdrantCapability::Brokered {
            operations: vec![operation(
                "invalidate_projection",
                "POST",
                "/api/code-index/invalidate",
            )],
        };
    }
    QdrantCapability::Direct {
        url: url.to_owned(),
        api_key,
    }
}

fn falkor_operations() -> Vec<BrokerOperation> {
    vec![
        operation("clear_projection", "POST", "/api/code-index/graph/clear"),
        operation(
            "rebuild_projection",
            "POST",
            "/api/code-index/graph/rebuild",
        ),
    ]
}

fn operation(name: &str, method: &str, path: &str) -> BrokerOperation {
    BrokerOperation {
        name: name.to_owned(),
        method: method.to_owned(),
        path: path.to_owned(),
    }
}

fn ai(enabled: bool) -> AiCapability {
    if enabled {
        AiCapability::Daemon {}
    } else {
        AiCapability::Unavailable {}
    }
}

fn vision_extract_enabled(values: &Map<String, Value>) -> bool {
    values.iter().any(|(key, value)| {
        key.starts_with("ai.generation.endpoints.")
            && key.ends_with(".input_modalities")
            && value.as_array().is_some_and(|modalities| {
                modalities.iter().any(|mode| mode.as_str() == Some("image"))
            })
    })
}

fn audio_transcribe_enabled(values: &Map<String, Value>) -> bool {
    if truthy(values.get("voice.enabled")) && truthy(values.get("voice.stt_enabled")) {
        return true;
    }
    values
        .get("voice.openai_compatible_audio")
        .and_then(Value::as_array)
        .is_some_and(|bindings| {
            bindings
                .iter()
                .any(|binding| truthy(binding.get("transcription_enabled")))
        })
}

/// Python's `_url_requires_broker`: a missing, unresolved, unparseable, non-HTTP, or
/// credential-bearing URL is not a direct connection.
fn url_requires_broker(url: Option<&str>) -> bool {
    match url {
        None => true,
        Some(url) if url.is_empty() || unresolved(url) => true,
        Some(url) => match Url::parse(url) {
            Err(_) => true,
            Ok(parsed) => {
                !matches!(parsed.scheme(), "http" | "https")
                    || parsed.host_str().is_none()
                    || !parsed.username().is_empty()
                    || parsed.password().is_some()
            }
        },
    }
}

/// Python's `_is_loopback_host`: whitespace and brackets stripped, compared lowercase.
fn is_loopback(host: &str) -> bool {
    matches!(
        host.trim()
            .trim_matches(['[', ']'])
            .to_ascii_lowercase()
            .as_str(),
        "127.0.0.1" | "localhost" | "::1"
    )
}

/// Python's `_unresolved`: a `$secret:` reference or an env placeholder.
fn unresolved(value: &str) -> bool {
    value.contains("$secret:") || value.contains("${")
}

fn text<'a>(values: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    values.get(key).and_then(Value::as_str)
}

fn truthy(value: Option<&Value>) -> bool {
    value.and_then(Value::as_bool).unwrap_or(false)
}

/// The opened plaintext of an active secret binding, when one is active.
fn resolved_secret(secrets: &BTreeMap<String, Option<String>>, key: &str) -> Option<String> {
    secrets.get(key).cloned().flatten()
}

/// A literal config value, used when no secret binding supplies one.
fn plain(value: Option<&str>) -> Option<&str> {
    value.filter(|value| !value.is_empty() && !unresolved(value))
}

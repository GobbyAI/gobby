use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

use crate::config::EmbeddingConfig;
use crate::models::Symbol;

use super::types::VectorLifecycleError;

const DIMENSION_PROBE_TEXT: &str = "dimension_probe";
static EMBEDDING_CLIENTS: OnceLock<Mutex<HashMap<u64, reqwest::blocking::Client>>> =
    OnceLock::new();

pub(super) fn dimension_probe_text() -> &'static str {
    DIMENSION_PROBE_TEXT
}

pub fn embedding_client(
    config: &EmbeddingConfig,
) -> Result<reqwest::blocking::Client, VectorLifecycleError> {
    let mut clients = EMBEDDING_CLIENTS
        .get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
        .map_err(|err| VectorLifecycleError::EmbeddingResponse(err.to_string()))?;
    // The blocking HTTP client is keyed only by timeout because request-specific
    // embedding endpoint, model, and auth details are applied per request.
    if let Some(client) = clients.get(&config.timeout_seconds) {
        return Ok(client.clone());
    }

    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(config.timeout_seconds))
        .build()
        .map_err(|err| VectorLifecycleError::EmbeddingResponse(err.to_string()))?;
    clients.insert(config.timeout_seconds, client.clone());
    Ok(client)
}

pub fn embed_text(
    client: &reqwest::blocking::Client,
    config: &EmbeddingConfig,
    text: &str,
) -> Result<Vec<f32>, VectorLifecycleError> {
    let body = json!({
        "model": config.model,
        "input": text,
    });

    let data = send_embedding_request(client, config, body)?;
    data.get("data")
        .and_then(Value::as_array)
        .and_then(|values| values.first())
        .ok_or_else(|| {
            VectorLifecycleError::EmbeddingResponse("missing data[0] object".to_string())
        })
        .and_then(parse_embedding)
}

pub fn probe_embedding_dim(config: &EmbeddingConfig) -> Result<usize, VectorLifecycleError> {
    let client = embedding_client(config)?;
    Ok(embed_text(&client, config, dimension_probe_text())?.len())
}

pub fn embed_text_batch(
    client: &reqwest::blocking::Client,
    config: &EmbeddingConfig,
    texts: &[String],
) -> Result<Vec<Vec<f32>>, VectorLifecycleError> {
    if texts.is_empty() {
        return Ok(Vec::new());
    }
    let body = json!({
        "model": config.model,
        "input": texts,
    });

    let data = send_embedding_request(client, config, body)?;
    let data = data
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| VectorLifecycleError::EmbeddingResponse("missing data array".to_string()))?;
    if data.len() != texts.len() {
        return Err(VectorLifecycleError::EmbeddingResponse(format!(
            "embedding response returned {} vector(s) for {} input(s)",
            data.len(),
            texts.len()
        )));
    }

    let mut ordered = vec![None; texts.len()];
    for (position, item) in data.iter().enumerate() {
        let index = item
            .get("index")
            .and_then(Value::as_u64)
            .and_then(|index| usize::try_from(index).ok())
            .unwrap_or(position);
        if index >= texts.len() || ordered[index].is_some() {
            return Err(VectorLifecycleError::EmbeddingResponse(
                "embedding response contained an invalid index".to_string(),
            ));
        }
        ordered[index] = Some(parse_embedding(item)?);
    }

    ordered
        .into_iter()
        .map(|embedding| {
            embedding.ok_or_else(|| {
                VectorLifecycleError::EmbeddingResponse(
                    "embedding response omitted an input index".to_string(),
                )
            })
        })
        .collect()
}

fn send_embedding_request(
    client: &reqwest::blocking::Client,
    config: &EmbeddingConfig,
    body: Value,
) -> Result<Value, VectorLifecycleError> {
    let url = format!("{}/embeddings", config.api_base.trim_end_matches('/'));
    let mut req = client.post(&url).json(&body);

    if let Some(key) = &config.api_key {
        req = req.header("Authorization", format!("Bearer {key}"));
    }

    let resp = req
        .send()
        .map_err(|err| VectorLifecycleError::EmbeddingResponse(err.to_string()))?;
    if !resp.status().is_success() {
        let status = resp.status().as_u16();
        let body = resp.text().unwrap_or_default();
        return Err(VectorLifecycleError::EmbeddingHttp { status, body });
    }

    resp.json()
        .map_err(|err| VectorLifecycleError::EmbeddingResponse(err.to_string()))
}

fn parse_embedding(value: &Value) -> Result<Vec<f32>, VectorLifecycleError> {
    let embedding = value
        .get("embedding")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            VectorLifecycleError::EmbeddingResponse("missing embedding array".to_string())
        })?
        .iter()
        .map(|value| {
            value
                .as_f64()
                .map(|f| {
                    let converted = f as f32;
                    if f.is_finite() && converted.is_infinite() {
                        log::warn!(
                            "embedding value {f} overflowed f32 range and was stored as {converted}"
                        );
                    }
                    converted
                })
                .ok_or_else(|| {
                    VectorLifecycleError::EmbeddingResponse(
                        "embedding array contains a non-number".to_string(),
                    )
                })
        })
        .collect::<Result<Vec<_>, _>>()?;

    if embedding.is_empty() {
        return Err(VectorLifecycleError::EmbeddingResponse(
            "embedding vector was empty".to_string(),
        ));
    }
    Ok(embedding)
}

pub fn embed_query(config: &EmbeddingConfig, text: &str) -> Option<Vec<f32>> {
    let prefix = config.query_prefix.as_deref().unwrap_or("").trim();
    let input = if prefix.is_empty() {
        text.to_string()
    } else {
        format!("{prefix} {text}")
    };
    let client = match embedding_client(config) {
        Ok(client) => client,
        Err(error) => {
            eprintln!("gcode: query embedding failed: {error}");
            return None;
        }
    };
    match embed_text(&client, config, &input) {
        Ok(embedding) => Some(embedding),
        Err(error) => {
            eprintln!("gcode: query embedding failed: {error}");
            None
        }
    }
}

pub fn vector_text_for_symbol(symbol: &Symbol) -> String {
    let mut lines = vec![
        format!("name: {}", symbol.name),
        format!("qualified_name: {}", symbol.qualified_name),
        format!("kind: {}", symbol.kind),
        format!("language: {}", symbol.language),
        format!("file_path: {}", symbol.file_path),
        format!("range: {}-{}", symbol.line_start, symbol.line_end),
    ];
    if let Some(signature) = symbol
        .signature
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    {
        lines.push(format!("signature: {signature}"));
    }
    if let Some(docstring) = symbol
        .docstring
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    {
        lines.push(format!("docstring: {docstring}"));
    }
    if let Some(summary) = symbol
        .summary
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    {
        lines.push(format!("summary: {summary}"));
    }
    lines.join("\n")
}

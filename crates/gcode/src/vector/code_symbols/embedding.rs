use serde_json::{Value, json};
use std::time::Duration;

use crate::config::EmbeddingConfig;
use crate::models::Symbol;

use super::types::VectorLifecycleError;

const HTTP_TIMEOUT: Duration = Duration::from_secs(10);
const DIMENSION_PROBE_TEXT: &str = "dimension_probe";

pub(super) fn dimension_probe_text() -> &'static str {
    DIMENSION_PROBE_TEXT
}

pub fn embed_text(config: &EmbeddingConfig, text: &str) -> Result<Vec<f32>, VectorLifecycleError> {
    let client = reqwest::blocking::Client::builder()
        .timeout(HTTP_TIMEOUT)
        .build()
        .map_err(|err| VectorLifecycleError::EmbeddingResponse(err.to_string()))?;

    let body = json!({
        "model": config.model,
        "input": text,
    });

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

    let data: Value = resp
        .json()
        .map_err(|err| VectorLifecycleError::EmbeddingResponse(err.to_string()))?;
    let embedding: Vec<f32> = data
        .get("data")
        .and_then(Value::as_array)
        .and_then(|values| values.first())
        .and_then(|value| value.get("embedding"))
        .and_then(Value::as_array)
        .ok_or_else(|| {
            VectorLifecycleError::EmbeddingResponse("missing data[0].embedding array".to_string())
        })?
        .iter()
        .map(|value| {
            value.as_f64().map(|f| f as f32).ok_or_else(|| {
                VectorLifecycleError::EmbeddingResponse(
                    "embedding array contains a non-number".to_string(),
                )
            })
        })
        .collect::<Result<Vec<_>, _>>()?;

    if embedding.is_empty() {
        Err(VectorLifecycleError::EmbeddingResponse(
            "embedding vector was empty".to_string(),
        ))
    } else {
        Ok(embedding)
    }
}

pub fn embed_query(config: &EmbeddingConfig, text: &str) -> Option<Vec<f32>> {
    match embed_text(config, &format!("search_query: {text}")) {
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

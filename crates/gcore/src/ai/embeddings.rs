//! Blocking OpenAI-compatible embeddings client for direct (non-daemon) routes.
//!
//! POSTs to `{api_base}/embeddings` with optional bearer auth and a per-request
//! timeout taken from [`EmbeddingConfig::timeout_seconds`] (the request timeout
//! overrides any client-level default). The daemon route lives in
//! [`super::daemon::embed_via_daemon`]; consumers map [`AiError`] into their own
//! error vocabulary at the boundary.

use std::time::Duration;

pub use reqwest::blocking::Client;
use serde_json::{Value, json};

use crate::ai_types::AiError;
use crate::config::EmbeddingConfig;

/// Embed one text. Sends `input` as a JSON string and returns
/// `data[0].embedding`.
pub fn embed_one(
    client: &Client,
    config: &EmbeddingConfig,
    text: &str,
) -> Result<Vec<f32>, AiError> {
    let response = send_request(
        client,
        config,
        json!({
            "model": config.model,
            "input": text,
        }),
    )?;
    let item = response
        .get("data")
        .and_then(Value::as_array)
        .and_then(|data| data.first())
        .ok_or_else(|| AiError::parse_failure("embedding response is missing data[0]"))?;
    parse_embedding(item, 0)
}

/// Embed a batch. Sends `input` as a JSON array, honors the response `index`
/// field, and validates that every input position is covered exactly once.
pub fn embed_batch(
    client: &Client,
    config: &EmbeddingConfig,
    inputs: &[String],
) -> Result<Vec<Vec<f32>>, AiError> {
    if inputs.is_empty() {
        return Ok(Vec::new());
    }
    let response = send_request(
        client,
        config,
        json!({
            "model": config.model,
            "input": inputs,
        }),
    )?;
    let data = response
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| AiError::parse_failure("embedding response is missing the data array"))?;
    if data.len() != inputs.len() {
        return Err(AiError::parse_failure(format!(
            "embedding response returned {} vector(s) for {} input(s)",
            data.len(),
            inputs.len()
        )));
    }

    let mut ordered = vec![None; inputs.len()];
    for (position, item) in data.iter().enumerate() {
        let index = item
            .get("index")
            .and_then(Value::as_u64)
            .and_then(|index| usize::try_from(index).ok())
            .unwrap_or(position);
        if index >= inputs.len() || ordered[index].is_some() {
            return Err(AiError::parse_failure(
                "embedding response contained an invalid index",
            ));
        }
        ordered[index] = Some(parse_embedding(item, index)?);
    }

    ordered
        .into_iter()
        .map(|embedding| {
            embedding
                .ok_or_else(|| AiError::parse_failure("embedding response omitted an input index"))
        })
        .collect()
}

fn send_request(client: &Client, config: &EmbeddingConfig, body: Value) -> Result<Value, AiError> {
    let url = format!("{}/embeddings", config.api_base.trim_end_matches('/'));
    let mut request = client
        .post(url)
        .timeout(Duration::from_secs(config.timeout_seconds))
        .json(&body);
    if let Some(api_key) = &config.api_key {
        request = request.bearer_auth(api_key);
    }
    let response = request.send().map_err(super::reqwest_error)?;
    super::parse_json_response(response)
}

fn parse_embedding(item: &Value, index: usize) -> Result<Vec<f32>, AiError> {
    let embedding = item
        .get("embedding")
        .and_then(Value::as_array)
        .ok_or_else(|| AiError::parse_failure(format!("missing data[{index}].embedding array")))?
        .iter()
        .map(|value| {
            let number = value.as_f64().ok_or_else(|| {
                AiError::parse_failure(format!("data[{index}].embedding has a non-number"))
            })?;
            let converted = number as f32;
            if !number.is_finite() || converted.is_infinite() {
                return Err(AiError::parse_failure(format!(
                    "data[{index}].embedding has a value outside the f32 range"
                )));
            }
            Ok(converted)
        })
        .collect::<Result<Vec<_>, _>>()?;

    if embedding.is_empty() {
        return Err(AiError::parse_failure(format!(
            "data[{index}].embedding is empty"
        )));
    }
    Ok(embedding)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_http::{spawn_json_response, spawn_json_response_with_status};

    fn config(api_base: String, api_key: Option<&str>) -> EmbeddingConfig {
        EmbeddingConfig {
            api_base,
            model: "embed-small".to_string(),
            api_key: api_key.map(str::to_string),
            query_prefix: None,
            timeout_seconds: 5,
        }
    }

    #[test]
    fn embed_one_sends_string_input_with_bearer_auth() {
        let (base_url, handle) =
            spawn_json_response(r#"{"data": [{"embedding": [0.25, 0.5, 0.75]}]}"#)
                .expect("spawn server");
        let config = config(format!("{base_url}/v1"), Some("embedding-key"));

        let embedding =
            embed_one(&Client::new(), &config, "dimension_probe").expect("embedding response");
        let request = handle.join().expect("server thread").expect("request");

        assert_eq!(embedding, vec![0.25, 0.5, 0.75]);
        assert!(request.contains("POST /v1/embeddings HTTP/1.1"));
        assert!(request.contains("authorization: Bearer embedding-key"));
        assert!(request.contains(r#""model":"embed-small""#));
        assert!(request.contains(r#""input":"dimension_probe""#));
    }

    #[test]
    fn embed_batch_sends_array_input_and_reorders_by_index() {
        let (base_url, handle) = spawn_json_response(
            r#"{"data": [
                {"index": 1, "embedding": [0.2, 0.22]},
                {"index": 0, "embedding": [0.1, 0.11]}
            ]}"#,
        )
        .expect("spawn server");
        let config = config(format!("{base_url}/v1/"), None);

        let embeddings = embed_batch(
            &Client::new(),
            &config,
            &["first".to_string(), "second".to_string()],
        )
        .expect("batch response");
        let request = handle.join().expect("server thread").expect("request");

        assert_eq!(embeddings, vec![vec![0.1, 0.11], vec![0.2, 0.22]]);
        assert!(request.contains("POST /v1/embeddings HTTP/1.1"));
        assert!(request.contains(r#""input":["first","second"]"#));
    }

    #[test]
    fn embed_batch_with_no_inputs_skips_the_request() {
        let config = config("http://embeddings.invalid/v1".to_string(), None);
        let embeddings = embed_batch(&Client::new(), &config, &[]).expect("empty batch");
        assert!(embeddings.is_empty());
    }

    #[test]
    fn embed_batch_rejects_vector_count_mismatch() {
        let (base_url, handle) =
            spawn_json_response(r#"{"data": [{"embedding": [0.1]}]}"#).expect("spawn server");
        let config = config(base_url, None);

        let error = embed_batch(
            &Client::new(),
            &config,
            &["first".to_string(), "second".to_string()],
        )
        .expect_err("count mismatch");
        handle.join().expect("server thread").expect("request");

        assert!(matches!(
            &error,
            AiError::ParseFailure { source } if source.contains("1 vector(s) for 2 input(s)")
        ));
    }

    #[test]
    fn embed_batch_rejects_duplicate_index() {
        let (base_url, handle) = spawn_json_response(
            r#"{"data": [
                {"index": 0, "embedding": [0.1]},
                {"index": 0, "embedding": [0.2]}
            ]}"#,
        )
        .expect("spawn server");
        let config = config(base_url, None);

        let error = embed_batch(
            &Client::new(),
            &config,
            &["first".to_string(), "second".to_string()],
        )
        .expect_err("duplicate index");
        handle.join().expect("server thread").expect("request");

        assert!(matches!(
            &error,
            AiError::ParseFailure { source } if source.contains("invalid index")
        ));
    }

    #[test]
    fn non_success_status_surfaces_status_and_body() {
        let (base_url, handle) =
            spawn_json_response_with_status(404, r#"{"error": "no such model"}"#)
                .expect("spawn server");
        let config = config(base_url, None);

        let error = embed_one(&Client::new(), &config, "probe").expect_err("http error");
        handle.join().expect("server thread").expect("request");

        assert!(matches!(
            &error,
            AiError::HttpStatus { status: 404, body: Some(body) } if body.contains("no such model")
        ));
    }

    #[test]
    fn non_numeric_embedding_values_are_rejected() {
        let (base_url, handle) = spawn_json_response(r#"{"data": [{"embedding": [0.1, "oops"]}]}"#)
            .expect("spawn server");
        let config = config(base_url, None);

        let error = embed_one(&Client::new(), &config, "probe").expect_err("parse error");
        handle.join().expect("server thread").expect("request");

        assert!(matches!(
            &error,
            AiError::ParseFailure { source } if source.contains("data[0].embedding has a non-number")
        ));
    }
}

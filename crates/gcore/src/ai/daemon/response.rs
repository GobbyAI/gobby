use serde_json::Value;

use crate::ai_types::{AiError, TranscriptionResult};

use super::types::DaemonEmbeddingResult;

pub(super) fn parse_daemon_transcription(value: Value) -> Result<TranscriptionResult, AiError> {
    TranscriptionResult::from_wire_json(value)
}

pub(super) fn parse_daemon_embeddings(
    value: Value,
    expected_len: usize,
) -> Result<DaemonEmbeddingResult, AiError> {
    let model = value
        .get("model")
        .and_then(Value::as_str)
        .ok_or_else(|| AiError::parse_failure("daemon embedding response missing model"))?
        .to_string();
    let dim = value
        .get("dim")
        .and_then(Value::as_u64)
        .and_then(|dim| usize::try_from(dim).ok())
        .ok_or_else(|| AiError::parse_failure("daemon embedding response missing dim"))?;
    let embeddings = value
        .get("embeddings")
        .and_then(Value::as_array)
        .ok_or_else(|| AiError::parse_failure("daemon embedding response missing embeddings"))?;
    if embeddings.len() != expected_len {
        return Err(AiError::parse_failure(format!(
            "daemon embedding response returned {} vector(s) for {} input(s)",
            embeddings.len(),
            expected_len
        )));
    }

    let embeddings = embeddings
        .iter()
        .map(|embedding| parse_daemon_embedding(embedding, dim))
        .collect::<Result<Vec<_>, _>>()?;

    Ok(DaemonEmbeddingResult {
        embeddings,
        model,
        dim,
    })
}

fn parse_daemon_embedding(value: &Value, dim: usize) -> Result<Vec<f32>, AiError> {
    let vector = value
        .as_array()
        .ok_or_else(|| AiError::parse_failure("daemon embedding item is not an array"))?
        .iter()
        .map(|value| {
            value.as_f64().map(|value| value as f32).ok_or_else(|| {
                AiError::parse_failure("daemon embedding array contains a non-number")
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    if vector.len() != dim {
        return Err(AiError::parse_failure(format!(
            "daemon embedding returned {} dimension(s), expected {}",
            vector.len(),
            dim
        )));
    }
    Ok(vector)
}

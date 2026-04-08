//! Qdrant vector search + OpenAI-compatible embedding API.
//!
//! Provides semantic search via Qdrant REST API. Query embeddings are generated
//! by calling an OpenAI-compatible `/v1/embeddings` endpoint (LM Studio, Ollama,
//! OpenAI, etc.) — the same API the Gobby daemon uses for index-time embeddings.
//!
//! Graceful degradation:
//! - No embedding API configured → semantic search disabled (FTS5 + graph only)
//! - No Qdrant URL → semantic search disabled
//! - API call fails → semantic search disabled for that query

use serde_json::Value;

use crate::config::{Context, EmbeddingConfig, QdrantConfig};

// ── Query embedding (OpenAI-compatible HTTP API) ────────────────────

/// Embed a search query via OpenAI-compatible `/v1/embeddings` endpoint.
///
/// Returns None if the API is unreachable or returns an error (graceful degradation).
pub fn embed_query(config: &EmbeddingConfig, text: &str) -> Option<Vec<f32>> {
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .ok()?;

    let body = serde_json::json!({
        "model": config.model,
        "input": format!("search_query: {text}"),
    });

    let url = format!("{}/embeddings", config.api_base.trim_end_matches('/'));
    let mut req = client.post(&url).json(&body);

    if let Some(key) = &config.api_key {
        req = req.header("Authorization", format!("Bearer {key}"));
    }

    let resp = req.send().ok()?;
    if !resp.status().is_success() {
        return None;
    }

    let data: Value = resp.json().ok()?;
    let embedding: Vec<f32> = data
        .get("data")?
        .as_array()?
        .first()?
        .get("embedding")?
        .as_array()?
        .iter()
        .filter_map(|v| v.as_f64().map(|f| f as f32))
        .collect();

    if embedding.is_empty() {
        None
    } else {
        Some(embedding)
    }
}

// ── Qdrant REST API (read-only) ─────────────────────────────────────

/// Search Qdrant for similar vectors. Returns (point_id, score) pairs.
pub fn vector_search(
    config: &QdrantConfig,
    collection: &str,
    query_vector: &[f32],
    limit: usize,
) -> anyhow::Result<Vec<(String, f64)>> {
    let url = match &config.url {
        Some(u) => u,
        None => return Ok(vec![]),
    };

    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()?;

    let body = serde_json::json!({
        "vector": query_vector,
        "limit": limit,
        "with_payload": false,
    });

    let mut req = client
        .post(format!("{url}/collections/{collection}/points/search"))
        .json(&body);

    if let Some(key) = &config.api_key {
        req = req.header("api-key", key);
    }

    let resp = req.send()?;
    if !resp.status().is_success() {
        return Ok(vec![]);
    }

    let data: Value = resp.json()?;
    let results = data
        .get("result")
        .and_then(|r| r.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|hit| {
                    let id = hit.get("id")?.as_str()?.to_string();
                    let score = hit.get("score")?.as_f64()?;
                    Some((id, score))
                })
                .collect()
        })
        .unwrap_or_default();

    Ok(results)
}

// ── Composite functions ──────────────────────────────────────────────

/// Run semantic search for a query. Returns (symbol_id, score) pairs.
///
/// Returns empty if Qdrant or embedding API unavailable.
pub fn semantic_search(ctx: &Context, query: &str, limit: usize) -> Vec<(String, f64)> {
    let qdrant_config = match &ctx.qdrant {
        Some(c) => c,
        None => return vec![],
    };

    let embedding_config = match &ctx.embedding {
        Some(c) => c,
        None => return vec![],
    };

    let embedding = match embed_query(embedding_config, query) {
        Some(e) => e,
        None => return vec![],
    };

    let collection = format!("{}{}", qdrant_config.collection_prefix, ctx.project_id);

    vector_search(qdrant_config, &collection, &embedding, limit).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn make_ctx_no_qdrant() -> Context {
        Context {
            db_path: PathBuf::from("/nonexistent"),
            project_root: PathBuf::from("/nonexistent"),
            project_id: "test".to_string(),
            quiet: true,
            neo4j: None,
            qdrant: None,
            embedding: None,
            daemon_url: None,
        }
    }

    #[test]
    fn test_semantic_search_no_qdrant() {
        let ctx = make_ctx_no_qdrant();
        let result = semantic_search(&ctx, "test query", 10);
        assert!(result.is_empty());
    }

    #[test]
    fn test_semantic_search_no_embedding_config() {
        let ctx = Context {
            qdrant: Some(QdrantConfig {
                url: Some("http://localhost:6333".to_string()),
                api_key: None,
                collection_prefix: "code_symbols_".to_string(),
            }),
            ..make_ctx_no_qdrant()
        };
        // No embedding config → returns empty
        let result = semantic_search(&ctx, "test query", 10);
        assert!(result.is_empty());
    }
}

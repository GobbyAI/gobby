use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::config::{Context, EmbeddingConfig, QdrantConfig};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeSymbolVectorSearchRequest {
    pub project_id: String,
    pub query: String,
    pub limit: usize,
    pub collection_prefix: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CodeSymbolVectorSearchHit {
    pub symbol_id: String,
    pub score: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CodeSymbolVectorLifecycleAction {
    Ensure,
    Clear,
    Rebuild,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CodeSymbolVectorLifecycleStatus {
    pub project_id: String,
    pub collection: String,
    pub action: CodeSymbolVectorLifecycleAction,
}

pub fn collection_name(collection_prefix: &str, project_id: &str) -> String {
    format!("{collection_prefix}{project_id}")
}

pub fn lifecycle_status(
    project_id: impl Into<String>,
    collection_prefix: &str,
    action: CodeSymbolVectorLifecycleAction,
) -> CodeSymbolVectorLifecycleStatus {
    let project_id = project_id.into();
    CodeSymbolVectorLifecycleStatus {
        collection: collection_name(collection_prefix, &project_id),
        project_id,
        action,
    }
}

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

pub fn search_code_symbols(
    ctx: &Context,
    request: &CodeSymbolVectorSearchRequest,
) -> Vec<CodeSymbolVectorSearchHit> {
    let qdrant_config = match &ctx.qdrant {
        Some(c) => c,
        None => return vec![],
    };

    let embedding_config = match &ctx.embedding {
        Some(c) => c,
        None => return vec![],
    };

    let embedding = match embed_query(embedding_config, &request.query) {
        Some(e) => e,
        None => return vec![],
    };

    let collection = collection_name(&request.collection_prefix, &request.project_id);
    vector_search(qdrant_config, &collection, &embedding, request.limit)
        .unwrap_or_default()
        .into_iter()
        .map(|(symbol_id, score)| CodeSymbolVectorSearchHit { symbol_id, score })
        .collect()
}

pub fn semantic_search(ctx: &Context, query: &str, limit: usize) -> Vec<(String, f64)> {
    let qdrant_config = match &ctx.qdrant {
        Some(c) => c,
        None => return vec![],
    };

    let request = CodeSymbolVectorSearchRequest {
        project_id: ctx.project_id.clone(),
        query: query.to_string(),
        limit,
        collection_prefix: qdrant_config.collection_prefix.clone(),
    };

    search_code_symbols(ctx, &request)
        .into_iter()
        .map(|hit| (hit.symbol_id, hit.score))
        .collect()
}

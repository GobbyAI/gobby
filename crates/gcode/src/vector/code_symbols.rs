use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, Context, EmbeddingConfig, QdrantConfig};
use crate::models::{ProjectionMetadata, ProjectionProvenance, Symbol};
use gobby_core::qdrant::{CollectionScope, SearchRequest};

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

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CodeSymbolVectorPayload {
    pub project_id: String,
    pub file_path: String,
    pub symbol_id: String,
    pub name: String,
    pub kind: String,
    pub language: String,
    pub line_start: usize,
    pub line_end: usize,
    pub byte_start: usize,
    pub byte_end: usize,
    pub provenance: ProjectionProvenance,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub confidence: Option<f64>,
    pub source_system: String,
    pub source_file_path: String,
    pub source_line: usize,
    pub source_symbol_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub summary: Option<String>,
}

impl CodeSymbolVectorPayload {
    pub fn from_symbol(symbol: &Symbol) -> Self {
        let metadata = ProjectionMetadata::gcode_extracted()
            .with_source_file_path(&symbol.file_path)
            .with_source_line(symbol.line_start)
            .with_source_symbol_id(&symbol.id);

        Self {
            project_id: symbol.project_id.clone(),
            file_path: symbol.file_path.clone(),
            symbol_id: symbol.id.clone(),
            name: symbol.name.clone(),
            kind: symbol.kind.clone(),
            language: symbol.language.clone(),
            line_start: symbol.line_start,
            line_end: symbol.line_end,
            byte_start: symbol.byte_start,
            byte_end: symbol.byte_end,
            provenance: metadata.provenance,
            confidence: metadata.confidence,
            source_system: metadata.source_system,
            source_file_path: metadata
                .source_file_path
                .unwrap_or_else(|| symbol.file_path.clone()),
            source_line: metadata.source_line.unwrap_or(symbol.line_start),
            source_symbol_id: metadata
                .source_symbol_id
                .unwrap_or_else(|| symbol.id.clone()),
            summary: symbol.summary.clone(),
        }
    }
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
    let collection = format!("{collection_prefix}{project_id}");
    gobby_core::qdrant::collection_name("gcode", CollectionScope::Custom(&collection))
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
    let request = SearchRequest {
        vector: query_vector.to_vec(),
        limit,
        filter: None,
    };
    let (hits, _) = gobby_core::qdrant::with_qdrant(Some(config), Vec::new(), |config| {
        gobby_core::qdrant::search(config, collection, request)
    })?;
    Ok(hits
        .into_iter()
        .map(|hit| (hit.id, f64::from(hit.score)))
        .collect())
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
    if ctx.qdrant.is_none() {
        return vec![];
    }

    let request = CodeSymbolVectorSearchRequest {
        project_id: ctx.project_id.clone(),
        query: query.to_string(),
        limit,
        collection_prefix: CODE_SYMBOL_COLLECTION_PREFIX.to_string(),
    };

    search_code_symbols(ctx, &request)
        .into_iter()
        .map(|hit| (hit.symbol_id, hit.score))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::{SOURCE_SYSTEM_GCODE, Symbol};

    fn test_symbol(summary: Option<String>) -> Symbol {
        Symbol {
            id: "symbol-1".to_string(),
            project_id: "project-1".to_string(),
            file_path: "src/lib.rs".to_string(),
            name: "run".to_string(),
            qualified_name: "crate::run".to_string(),
            kind: "function".to_string(),
            language: "rust".to_string(),
            byte_start: 10,
            byte_end: 40,
            line_start: 3,
            line_end: 5,
            signature: None,
            docstring: None,
            parent_symbol_id: None,
            content_hash: "hash".to_string(),
            summary,
            created_at: String::new(),
            updated_at: String::new(),
        }
    }

    #[test]
    fn payloads_use_gcode_provenance_and_existing_summary() {
        let payload = CodeSymbolVectorPayload::from_symbol(&test_symbol(Some("does work".into())));

        assert_eq!(payload.provenance, ProjectionProvenance::Extracted);
        assert_eq!(payload.confidence, Some(1.0));
        assert_eq!(payload.source_system, SOURCE_SYSTEM_GCODE);
        assert_eq!(payload.source_file_path, "src/lib.rs");
        assert_eq!(payload.source_line, 3);
        assert_eq!(payload.source_symbol_id, "symbol-1");
        assert_eq!(payload.summary.as_deref(), Some("does work"));

        let without_summary =
            serde_json::to_value(CodeSymbolVectorPayload::from_symbol(&test_symbol(None)))
                .expect("payload serializes");
        assert!(without_summary.get("summary").is_none());
    }
}

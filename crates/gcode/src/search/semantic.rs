//! Qdrant vector search + llama-cpp-2 GGUF embeddings.
//!
//! Provides semantic search via Qdrant REST API and local embedding generation
//! using the nomic-embed-text-v1.5 GGUF model.
//!
//! Graceful degradation:
//! - No GGUF model → semantic search disabled (FTS5 + graph only)
//! - No Qdrant URL → semantic search disabled
//! - No `embeddings` feature → semantic search disabled at compile time
//!
//! Source: src/gobby/search/local_embeddings.py, src/gobby/code_index/searcher.py

use serde_json::Value;

use crate::config::{Context, QdrantConfig};

/// Embedding dimension for nomic-embed-text-v1.5.
#[cfg(feature = "embeddings")]
const EMBEDDING_DIM: usize = 768;

// ── Embedding model (requires `embeddings` feature) ─────────────────

#[cfg(feature = "embeddings")]
mod embedding_impl {
    use std::path::PathBuf;
    use std::sync::Mutex;

    use llama_cpp_2::context::params::LlamaContextParams;
    use llama_cpp_2::llama_backend::LlamaBackend;
    use llama_cpp_2::llama_batch::LlamaBatch;
    use llama_cpp_2::model::params::LlamaModelParams;
    use llama_cpp_2::model::{AddBos, LlamaModel};

    use super::EMBEDDING_DIM;

    /// Thread-safe embedding model wrapper.
    /// Uses Mutex because llama.cpp is not thread-safe.
    struct EmbeddingModelInner {
        #[allow(dead_code)]
        backend: LlamaBackend,
        model: LlamaModel,
    }

    static EMBEDDING_MODEL: Mutex<Option<EmbeddingModelInner>> = Mutex::new(None);

    /// Model file path.
    fn model_path() -> Option<PathBuf> {
        let path = dirs::home_dir()?.join(".gobby/models/nomic-embed-text-v1.5.Q8_0.gguf");
        if path.exists() { Some(path) } else { None }
    }

    /// Initialize the embedding model (lazy, called once).
    fn ensure_model_loaded() -> bool {
        let mut guard = EMBEDDING_MODEL.lock().unwrap();
        if guard.is_some() {
            return true;
        }

        let path = match model_path() {
            Some(p) => p,
            None => return false,
        };

        let backend = match LlamaBackend::init() {
            Ok(b) => b,
            Err(e) => {
                eprintln!("Warning: failed to init llama backend: {e}");
                return false;
            }
        };

        let model_params = LlamaModelParams::default().with_n_gpu_layers(u32::MAX); // Metal/CUDA auto-detect

        match LlamaModel::load_from_file(&backend, &path, &model_params) {
            Ok(model) => {
                *guard = Some(EmbeddingModelInner { backend, model });
                true
            }
            Err(e) => {
                eprintln!("Warning: failed to load embedding model: {e}");
                false
            }
        }
    }

    /// Generate embedding for a single text.
    ///
    /// Applies nomic task prefixes: "search_query: " or "search_document: ".
    /// Returns None if model is not available.
    pub fn embed_text(text: &str, is_query: bool) -> Option<Vec<f32>> {
        if !ensure_model_loaded() {
            return None;
        }

        let prefix = if is_query {
            "search_query: "
        } else {
            "search_document: "
        };
        let prefixed = format!("{prefix}{text}");

        let guard = EMBEDDING_MODEL.lock().unwrap();
        let inner = guard.as_ref()?;

        let ctx_params = LlamaContextParams::default()
            .with_embeddings(true)
            .with_n_ctx(std::num::NonZeroU32::new(2048));

        let mut ctx = inner.model.new_context(&inner.backend, ctx_params).ok()?;

        // Tokenize
        let tokens = inner.model.str_to_token(&prefixed, AddBos::Always).ok()?;

        // Create batch and add tokens
        let mut batch = LlamaBatch::new(2048, 1);
        let last_idx = tokens.len().saturating_sub(1);
        for (i, &token) in tokens.iter().enumerate() {
            batch.add(token, i as i32, &[0], i == last_idx).ok()?;
        }

        // Encode (for embedding models, use encode not decode)
        ctx.encode(&mut batch).ok()?;

        // Extract sequence embedding (pooled)
        let embedding = ctx.embeddings_seq_ith(0).ok()?;

        if embedding.len() >= EMBEDDING_DIM {
            Some(embedding[..EMBEDDING_DIM].to_vec())
        } else {
            Some(embedding.to_vec())
        }
    }

    /// Batch embed multiple texts (for indexing).
    pub fn embed_texts(texts: &[String], is_query: bool) -> Vec<Option<Vec<f32>>> {
        texts.iter().map(|t| embed_text(t, is_query)).collect()
    }
}

#[cfg(feature = "embeddings")]
pub use embedding_impl::{embed_text, embed_texts};

#[cfg(not(feature = "embeddings"))]
pub fn embed_text(_text: &str, _is_query: bool) -> Option<Vec<f32>> {
    None
}

#[cfg(not(feature = "embeddings"))]
#[allow(dead_code)]
pub fn embed_texts(texts: &[String], _is_query: bool) -> Vec<Option<Vec<f32>>> {
    vec![None; texts.len()]
}

// ── Qdrant REST API ──────────────────────────────────────────────────

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

/// Upsert vectors to Qdrant for symbols.
pub fn upsert_vectors(
    config: &QdrantConfig,
    collection: &str,
    points: &[(String, Vec<f32>)],
) -> anyhow::Result<()> {
    if points.is_empty() {
        return Ok(());
    }

    let url = match &config.url {
        Some(u) => u,
        None => return Ok(()),
    };

    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()?;

    let qdrant_points: Vec<Value> = points
        .iter()
        .map(|(id, vector)| {
            serde_json::json!({
                "id": id,
                "vector": vector,
            })
        })
        .collect();

    let body = serde_json::json!({ "points": qdrant_points });

    let mut req = client
        .put(format!("{url}/collections/{collection}/points"))
        .json(&body);

    if let Some(key) = &config.api_key {
        req = req.header("api-key", key);
    }

    let _ = req.send()?;
    Ok(())
}

// ── Composite functions ──────────────────────────────────────────────

/// Run semantic search for a query. Returns (symbol_id, score) pairs.
///
/// Returns empty if Qdrant or embedding model unavailable.
pub fn semantic_search(ctx: &Context, query: &str, limit: usize) -> Vec<(String, f64)> {
    let config = match &ctx.qdrant {
        Some(c) => c,
        None => return vec![],
    };

    let embedding = match embed_text(query, true) {
        Some(e) => e,
        None => return vec![],
    };

    let collection = format!("{}{}", config.collection_prefix, ctx.project_id);

    vector_search(config, &collection, &embedding, limit).unwrap_or_default()
}

/// Build embedding text for a symbol (name + signature + docstring).
pub fn symbol_embed_text(sym: &crate::models::Symbol) -> String {
    let mut text = sym.qualified_name.clone();
    if let Some(sig) = &sym.signature {
        text.push(' ');
        text.push_str(sig);
    }
    if let Some(doc) = &sym.docstring {
        text.push(' ');
        text.push_str(&doc[..doc.len().min(500)]);
    }
    text
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
        }
    }

    #[test]
    fn test_semantic_search_no_qdrant() {
        let ctx = make_ctx_no_qdrant();
        let result = semantic_search(&ctx, "test query", 10);
        assert!(result.is_empty());
    }

    #[test]
    fn test_semantic_search_no_model() {
        let ctx = Context {
            qdrant: Some(QdrantConfig {
                url: Some("http://localhost:6333".to_string()),
                api_key: None,
                collection_prefix: "code_symbols_".to_string(),
            }),
            ..make_ctx_no_qdrant()
        };
        // Model won't exist in test env → returns empty
        let result = semantic_search(&ctx, "test query", 10);
        assert!(result.is_empty());
    }

    #[test]
    fn test_symbol_embed_text() {
        let sym = crate::models::Symbol {
            id: "id".into(),
            project_id: "p".into(),
            file_path: "f.py".into(),
            name: "foo".into(),
            qualified_name: "module.foo".into(),
            kind: "function".into(),
            language: "python".into(),
            byte_start: 0,
            byte_end: 100,
            line_start: 1,
            line_end: 10,
            signature: Some("def foo(x: int) -> str".into()),
            docstring: Some("Do the thing.".into()),
            parent_symbol_id: None,
            content_hash: String::new(),
            summary: None,
            created_at: String::new(),
            updated_at: String::new(),
        };
        let text = symbol_embed_text(&sym);
        assert!(text.contains("module.foo"));
        assert!(text.contains("def foo"));
        assert!(text.contains("Do the thing"));
    }
}

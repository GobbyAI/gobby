use super::*;
use crate::search::{SearchScope, SearchSource};
use gobby_core::config::{EmbeddingConfig, QdrantConfig};
use gobby_core::degradation::{DegradationKind, ServiceState};
use serde_json::json;

#[test]
fn semantic_search_is_scope_filtered() {
    let embedding = EmbeddingConfig {
        api_base: "http://embeddings.local/v1".to_string(),
        model: "embed-model".to_string(),
        api_key: None,
        query_prefix: Some("query: ".to_string()),
        timeout_seconds: 10,
    };
    let qdrant = QdrantConfig {
        url: Some("http://qdrant.local".to_string()),
        api_key: None,
    };
    let mut embedder = FixedEmbedder::new(vec![0.1, 0.2, 0.3]);
    let mut vector = RecordingVectorBackend::new(vec![
        vector_hit("doc-1", "project", "project-1"),
        vector_hit("doc-2", "topic", "rust"),
    ]);

    let outcome = search_semantic(
        SemanticSearchRequest {
            query: "ownership".to_string(),
            scope: SearchScope::project("project-1"),
            limit: 5,
        },
        Some(&SemanticEmbedding::Direct(embedding)),
        Some(&qdrant),
        &mut embedder,
        &mut vector,
    )
    .expect("semantic search succeeds");

    assert_eq!(outcome.hits.len(), 1);
    assert_eq!(outcome.hits[0].id, "doc-1");
    assert_eq!(outcome.hits[0].sources, vec![SearchSource::Semantic]);
    assert!(outcome.degradation.is_none());
    assert_eq!(embedder.queries, vec!["query: ownership"]);

    assert_eq!(
        vector.collection,
        Some("gwiki_project_project-1".to_string())
    );
    assert_eq!(
        vector.filter,
        Some(json!({
            "must": [
                {"key": "namespace", "match": {"value": "gwiki"}},
                {"key": "scope_kind", "match": {"value": "project"}},
                {"key": "project_id", "match": {"value": "project-1"}}
            ]
        }))
    );
}

#[test]
fn semantic_search_requires_embedding_and_qdrant_config() {
    let qdrant = QdrantConfig {
        url: Some("http://qdrant.local".to_string()),
        api_key: None,
    };
    let mut embedder = FixedEmbedder::new(vec![0.1, 0.2, 0.3]);
    let mut vector = RecordingVectorBackend::new(Vec::new());

    let missing_embedding = search_semantic(
        SemanticSearchRequest {
            query: "ownership".to_string(),
            scope: SearchScope::project("project-1"),
            limit: 5,
        },
        None,
        Some(&qdrant),
        &mut embedder,
        &mut vector,
    )
    .expect_err("missing embedding config must fail");
    assert!(
        missing_embedding
            .to_string()
            .contains("requires embeddings")
    );

    let embedding = EmbeddingConfig {
        api_base: "http://embeddings.local/v1".to_string(),
        model: "embed-model".to_string(),
        api_key: None,
        query_prefix: None,
        timeout_seconds: 10,
    };
    let missing_qdrant = search_semantic(
        SemanticSearchRequest {
            query: "ownership".to_string(),
            scope: SearchScope::project("project-1"),
            limit: 5,
        },
        Some(&SemanticEmbedding::Direct(embedding)),
        None,
        &mut embedder,
        &mut vector,
    )
    .expect_err("missing Qdrant config must fail");
    assert!(missing_qdrant.to_string().contains("requires qdrant"));
}

#[test]
fn semantic_search_global_scope_degrades_without_fake_collection() {
    let mut embedder = FixedEmbedder::new(vec![0.1, 0.2, 0.3]);
    let mut vector = RecordingVectorBackend::new(Vec::new());

    let outcome = search_semantic(
        SemanticSearchRequest {
            query: "ownership".to_string(),
            scope: SearchScope::global(),
            limit: 5,
        },
        None,
        None,
        &mut embedder,
        &mut vector,
    )
    .expect("global semantic search degrades");

    assert!(outcome.hits.is_empty());
    assert!(matches!(
        outcome.degradation,
        Some(DegradationKind::ServiceUnavailable {
            service,
            state: ServiceState::Unreachable { .. },
        }) if service == "qdrant"
    ));
    assert_eq!(vector.collection, None);
}

#[test]
fn semantic_search_degrades_configured_embedding_failure() {
    let embedding = EmbeddingConfig {
        api_base: "http://embeddings.local/v1".to_string(),
        model: "embed-model".to_string(),
        api_key: None,
        query_prefix: None,
        timeout_seconds: 10,
    };
    let qdrant = QdrantConfig {
        url: Some("http://qdrant.local".to_string()),
        api_key: None,
    };
    let mut embedder = FailingEmbedder;
    let mut vector = RecordingVectorBackend::new(Vec::new());

    let outcome = search_semantic(
        SemanticSearchRequest {
            query: "ownership".to_string(),
            scope: SearchScope::project("project-1"),
            limit: 5,
        },
        Some(&SemanticEmbedding::Direct(embedding)),
        Some(&qdrant),
        &mut embedder,
        &mut vector,
    )
    .expect("configured embedding failure degrades");

    assert!(outcome.hits.is_empty());
    assert!(matches!(
        outcome.degradation,
        Some(DegradationKind::ServiceUnavailable {
            service,
            state: ServiceState::Unreachable { message }
        }) if service == "embeddings" && message.contains("embedding timeout")
    ));
}

#[test]
fn semantic_search_degrades_configured_qdrant_failure() {
    let embedding = EmbeddingConfig {
        api_base: "http://embeddings.local/v1".to_string(),
        model: "embed-model".to_string(),
        api_key: None,
        query_prefix: None,
        timeout_seconds: 10,
    };
    let qdrant = QdrantConfig {
        url: Some("http://qdrant.local".to_string()),
        api_key: None,
    };
    let mut embedder = FixedEmbedder::new(vec![0.1, 0.2, 0.3]);
    let mut vector = FailingVectorBackend;

    let outcome = search_semantic(
        SemanticSearchRequest {
            query: "ownership".to_string(),
            scope: SearchScope::project("project-1"),
            limit: 5,
        },
        Some(&SemanticEmbedding::Direct(embedding)),
        Some(&qdrant),
        &mut embedder,
        &mut vector,
    )
    .expect("configured qdrant failure degrades");

    assert!(outcome.hits.is_empty());
    assert!(matches!(
        outcome.degradation,
        Some(DegradationKind::ServiceUnavailable {
            service,
            state: ServiceState::Unreachable { message }
        }) if service == "qdrant" && message.contains("qdrant timeout")
    ));
}

#[test]
fn semantic_search_classifies_qdrant_collection_and_auth_failures() {
    let embedding = EmbeddingConfig {
        api_base: "http://embeddings.local/v1".to_string(),
        model: "embed-model".to_string(),
        api_key: None,
        query_prefix: None,
        timeout_seconds: 10,
    };
    let qdrant = QdrantConfig {
        url: Some("http://qdrant.local".to_string()),
        api_key: None,
    };

    let mut embedder = FixedEmbedder::new(vec![0.1, 0.2, 0.3]);
    let mut missing_collection = QdrantStatusVectorBackend {
        status: gobby_core::qdrant::StatusCode::NOT_FOUND,
    };
    let outcome = search_semantic(
        SemanticSearchRequest {
            query: "ownership".to_string(),
            scope: SearchScope::project("project-1"),
            limit: 5,
        },
        Some(&SemanticEmbedding::Direct(embedding.clone())),
        Some(&qdrant),
        &mut embedder,
        &mut missing_collection,
    )
    .expect("missing collection degrades");
    assert!(matches!(
        outcome.degradation,
        Some(DegradationKind::ServiceUnavailable {
            service,
            state: ServiceState::NotConfigured
        }) if service == "qdrant_collection"
    ));

    let mut embedder = FixedEmbedder::new(vec![0.1, 0.2, 0.3]);
    let mut auth_failure = QdrantStatusVectorBackend {
        status: gobby_core::qdrant::StatusCode::FORBIDDEN,
    };
    let outcome = search_semantic(
        SemanticSearchRequest {
            query: "ownership".to_string(),
            scope: SearchScope::project("project-1"),
            limit: 5,
        },
        Some(&SemanticEmbedding::Direct(embedding)),
        Some(&qdrant),
        &mut embedder,
        &mut auth_failure,
    )
    .expect("auth failure degrades");
    assert!(matches!(
        outcome.degradation,
        Some(DegradationKind::ServiceUnavailable {
            service,
            state: ServiceState::Unreachable { .. }
        }) if service == "qdrant_auth"
    ));
}

#[cfg(feature = "ai")]
#[test]
fn semantic_search_daemon_embedding_uses_raw_query() {
    let qdrant = QdrantConfig {
        url: Some("http://qdrant.local".to_string()),
        api_key: None,
    };
    let mut embedder = FixedEmbedder::new(vec![0.1, 0.2, 0.3]);
    let mut vector = RecordingVectorBackend::new(vec![vector_hit("doc-1", "project", "project-1")]);

    let outcome = search_semantic(
        SemanticSearchRequest {
            query: "ownership".to_string(),
            scope: SearchScope::project("project-1"),
            limit: 5,
        },
        Some(&SemanticEmbedding::Daemon(Box::new(test_ai_context()))),
        Some(&qdrant),
        &mut embedder,
        &mut vector,
    )
    .expect("semantic search succeeds");

    assert_eq!(outcome.hits.len(), 1);
    assert_eq!(embedder.queries, vec!["ownership"]);
}

#[cfg(feature = "ai")]
fn test_ai_context() -> AiContext {
    use gobby_core::ai_context::{AiBindings, AiLimiter};
    use gobby_core::config::{AiRouting, AiTuning, CapabilityBinding};

    let binding = CapabilityBinding {
        routing: AiRouting::Daemon,
        transport: None,
        api_base: None,
        api_key: None,
        model: None,
        provider: None,
        task: None,
        language: None,
        target_lang: None,
    };

    AiContext {
        bindings: AiBindings {
            embed: binding.clone(),
            audio_transcribe: binding.clone(),
            audio_translate: binding.clone(),
            vision_extract: binding.clone(),
            text_generate: binding,
        },
        tuning: AiTuning {
            max_concurrency: 1,
            keep_alive: None,
        },
        limiter: AiLimiter::new(1),
        project_id: Some("project-1".to_string()),
    }
}

fn vector_hit(id: &str, scope_kind: &str, scope_value: &str) -> gobby_core::qdrant::SearchHit {
    let mut payload = serde_json::Map::new();
    payload.insert("namespace".to_string(), json!("gwiki"));
    payload.insert("scope_kind".to_string(), json!(scope_kind));
    payload.insert("project_id".to_string(), json!(scope_value));
    payload.insert("topic".to_string(), json!(scope_value));
    payload.insert("path".to_string(), json!("knowledge/topics/rust.md"));
    payload.insert("source_path".to_string(), json!("raw/INDEX.md"));
    payload.insert("source_kind".to_string(), json!("topic"));
    payload.insert("content_hash".to_string(), json!("hash"));
    payload.insert("snippet".to_string(), json!("ownership and borrowing"));
    gobby_core::qdrant::SearchHit {
        id: id.to_string(),
        score: 0.8,
        payload,
    }
}

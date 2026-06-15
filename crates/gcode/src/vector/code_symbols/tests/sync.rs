use super::*;
use serde_json::json;

#[test]
fn sync_rejects_embedding_vectors_with_wrong_dimension() {
    let (embedding_url, embedding_handle) =
        spawn_http_responses(vec![(200, json!({"data": [{"embedding": [0.1, 0.2]}]}))]);
    let (qdrant_url, qdrant_handle) = spawn_http_responses(vec![(
        200,
        json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}}),
    )]);
    let mut lifecycle = CodeSymbolVectorLifecycle::new(
        "project-1".to_string(),
        QdrantConfig {
            url: Some(qdrant_url),
            api_key: None,
        },
        EmbeddingConfig {
            api_base: format!("{embedding_url}/v1"),
            model: "embed-small".to_string(),
            api_key: None,
            query_prefix: None,
            timeout_seconds: 10,
        },
        CodeVectorSettings {
            vector_dim: Some(3),
        },
    )
    .expect("lifecycle");

    let err = lifecycle
        .sync_file_symbols("src/lib.rs", &[test_symbol(None)])
        .expect_err("wrong vector dimension must fail before upsert");
    let embedding_requests = embedding_handle.join().expect("embedding requests");
    let qdrant_requests = qdrant_handle.join().expect("qdrant requests");

    assert!(
        matches!(err, VectorLifecycleError::EmbeddingResponse(message) if message.contains("expected 3"))
    );
    assert_eq!(embedding_requests.len(), 1);
    assert_eq!(qdrant_requests.len(), 1);
    assert!(!qdrant_requests[0].contains("/points"));
}

#[test]
fn sync_rejects_embedding_vector_count_mismatch() {
    let (embedding_url, embedding_handle) = spawn_http_responses(vec![(
        200,
        json!({"data": [{"embedding": [0.1, 0.2, 0.3]}]}),
    )]);
    let (qdrant_url, qdrant_handle) = spawn_http_responses(vec![(
        200,
        json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}}),
    )]);
    let mut lifecycle = CodeSymbolVectorLifecycle::new(
        "project-1".to_string(),
        QdrantConfig {
            url: Some(qdrant_url),
            api_key: None,
        },
        EmbeddingConfig {
            api_base: format!("{embedding_url}/v1"),
            model: "embed-small".to_string(),
            api_key: None,
            query_prefix: None,
            timeout_seconds: 10,
        },
        CodeVectorSettings {
            vector_dim: Some(3),
        },
    )
    .expect("lifecycle");
    let mut second = test_symbol(None);
    second.id = "symbol-2".to_string();

    let err = lifecycle
        .sync_file_symbols("src/lib.rs", &[test_symbol(None), second])
        .expect_err("vector count mismatch must fail before upsert");
    let embedding_requests = embedding_handle.join().expect("embedding requests");
    let qdrant_requests = qdrant_handle.join().expect("qdrant requests");

    let VectorLifecycleError::EmbeddingResponse(message) = &err else {
        panic!("unexpected error: {err}");
    };
    assert!(
        message.contains("1 vector(s) for 2 symbol(s)")
            || message.contains("1 vector(s) for 2 input(s)")
    );
    assert_eq!(embedding_requests.len(), 1);
    assert_eq!(qdrant_requests.len(), 1);
    assert!(!qdrant_requests[0].contains("/points"));
}

#[test]
fn sync_file_symbols_batches_embedding_and_qdrant_upsert() {
    let first_embedding_batch = (0..128)
        .map(|index| json!({"index": index, "embedding": [0.1, 0.2, 0.3]}))
        .collect::<Vec<_>>();
    let second_embedding_batch = vec![json!({"index": 0, "embedding": [0.4, 0.5, 0.6]})];
    let (embedding_url, embedding_handle) = spawn_http_responses(vec![
        (200, json!({"data": first_embedding_batch})),
        (200, json!({"data": second_embedding_batch})),
    ]);
    let (qdrant_url, qdrant_handle) = spawn_http_responses(vec![
        (
            200,
            json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}}),
        ),
        (
            200,
            json!({"result": {"operation_id": 17, "status": "completed"}, "status": "ok"}),
        ),
        (
            200,
            json!({"result": {"operation_id": 18, "status": "completed"}, "status": "ok"}),
        ),
        (200, json!({"result": {"count": 0}, "status": "ok"})),
    ]);
    let mut lifecycle = CodeSymbolVectorLifecycle::new(
        "project-1".to_string(),
        QdrantConfig {
            url: Some(qdrant_url),
            api_key: None,
        },
        EmbeddingConfig {
            api_base: format!("{embedding_url}/v1"),
            model: "embed-small".to_string(),
            api_key: None,
            query_prefix: None,
            timeout_seconds: 10,
        },
        CodeVectorSettings {
            vector_dim: Some(3),
        },
    )
    .expect("lifecycle");
    let symbols = (0..129).map(test_symbol_with_index).collect::<Vec<_>>();

    let output = lifecycle
        .sync_file_symbols("src/lib.rs", &symbols)
        .expect("sync file symbols");
    let embedding_requests = embedding_handle.join().expect("embedding requests");
    let qdrant_requests = qdrant_handle.join().expect("qdrant requests");
    let upsert_requests = qdrant_requests
        .iter()
        .filter(|request| request.contains("/points?wait=true"))
        .collect::<Vec<_>>();

    assert_eq!(output.vectors_upserted, 129);
    assert_eq!(embedding_requests.len(), 2);
    assert!(embedding_requests[0].contains("run_0"));
    assert!(embedding_requests[0].contains("run_127"));
    assert!(!embedding_requests[0].contains("run_128"));
    assert!(embedding_requests[1].contains("run_128"));
    assert_eq!(upsert_requests.len(), 2);
    assert!(upsert_requests[0].contains(r#""symbol-0""#));
    assert!(upsert_requests[0].contains(r#""symbol-127""#));
    assert!(!upsert_requests[0].contains(r#""symbol-128""#));
    assert!(upsert_requests[1].contains(r#""symbol-128""#));
}

#[test]
fn dim_probe_with_override() {
    let (embedding_url, embedding_handle) = spawn_http_responses(vec![(
        200,
        json!({"data": [{"embedding": [0.1, 0.2, 0.3]}]}),
    )]);
    let (qdrant_url, qdrant_handle) = spawn_http_responses(vec![
        (404, json!({"status": "not found"})),
        (200, json!({"result": true})),
        (
            200,
            json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}}),
        ),
    ]);
    let mut lifecycle = CodeSymbolVectorLifecycle::new(
        "project-1".to_string(),
        QdrantConfig {
            url: Some(qdrant_url),
            api_key: None,
        },
        EmbeddingConfig {
            api_base: format!("{embedding_url}/v1"),
            model: "embed-small".to_string(),
            api_key: None,
            query_prefix: None,
            timeout_seconds: 10,
        },
        CodeVectorSettings { vector_dim: None },
    )
    .expect("lifecycle");

    let created = lifecycle.ensure_collection().expect("create collection");
    let reused = lifecycle.ensure_collection().expect("reuse collection");
    let embedding_requests = embedding_handle.join().expect("embedding requests");
    let qdrant_requests = qdrant_handle.join().expect("qdrant requests");

    assert_eq!(created.size, 3);
    assert_eq!(created.distance, VECTOR_DISTANCE_COSINE);
    assert_eq!(reused.size, 3);
    assert_eq!(embedding_requests.len(), 1, "dimension probe is cached");
    assert!(qdrant_requests[1].contains("PUT /collections/code_symbols_project-1 HTTP/1.1"));
    assert!(qdrant_requests[1].contains(r#""size":3"#));
    assert!(qdrant_requests[1].contains(r#""distance":"Cosine""#));

    let (explicit_qdrant_url, explicit_handle) = spawn_http_responses(vec![
        (404, json!({"status": "not found"})),
        (200, json!({"result": true})),
    ]);
    let mut explicit = CodeSymbolVectorLifecycle::new(
        "project-1".to_string(),
        QdrantConfig {
            url: Some(explicit_qdrant_url),
            api_key: None,
        },
        EmbeddingConfig {
            api_base: "http://127.0.0.1:9/v1".to_string(),
            model: "unused".to_string(),
            api_key: None,
            query_prefix: None,
            timeout_seconds: 10,
        },
        CodeVectorSettings {
            vector_dim: Some(1536),
        },
    )
    .expect("lifecycle with explicit size");

    let schema = explicit.ensure_collection().expect("explicit size create");
    let explicit_requests = explicit_handle.join().expect("explicit qdrant requests");
    assert_eq!(schema.size, 1536);
    assert!(explicit_requests[1].contains(r#""size":1536"#));
}

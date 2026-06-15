use super::*;
use serde_json::json;

#[test]
fn delete_file_vectors_filters_by_project_and_file_without_embedding() {
    let (qdrant_url, handle) = spawn_http_responses(vec![
        (200, json!({"result": {"count": 2}})),
        (200, json!({"result": {"operation_id": 1}})),
    ]);
    let deleted = delete_file_vectors(
        &QdrantConfig {
            url: Some(qdrant_url),
            api_key: Some("qdrant-key".to_string()),
        },
        "project-1",
        "src/lib.rs",
    )
    .expect("delete vectors");
    let requests = handle.join().expect("qdrant requests");

    assert_eq!(deleted, 2);
    assert_eq!(requests.len(), 2);
    assert!(requests[0].contains("POST /collections/code_symbols_project-1/points/count HTTP/1.1"));
    assert!(
        requests[1]
            .contains("POST /collections/code_symbols_project-1/points/delete?wait=true HTTP/1.1")
    );
    assert!(
        requests
            .iter()
            .all(|request| request.contains("api-key: qdrant-key"))
    );
    assert!(
        requests
            .iter()
            .all(|request| request.contains(r#""key":"project_id""#))
    );
    assert!(
        requests
            .iter()
            .all(|request| request.contains(r#""value":"project-1""#))
    );
    assert!(
        requests
            .iter()
            .all(|request| request.contains(r#""key":"file_path""#))
    );
    assert!(
        requests
            .iter()
            .all(|request| request.contains(r#""value":"src/lib.rs""#))
    );
    assert!(requests[0].contains(r#""exact":true"#));
}

#[test]
fn delete_file_vectors_skips_delete_when_count_is_zero() {
    let (qdrant_url, handle) = spawn_http_responses(vec![(200, json!({"result": {"count": 0}}))]);
    let deleted = delete_file_vectors(
        &QdrantConfig {
            url: Some(qdrant_url),
            api_key: None,
        },
        "project-1",
        "src/lib.rs",
    )
    .expect("delete vectors");
    let requests = handle.join().expect("qdrant requests");

    assert_eq!(deleted, 0);
    assert_eq!(requests.len(), 1);
    assert!(requests[0].contains("POST /collections/code_symbols_project-1/points/count HTTP/1.1"));
    assert!(!requests[0].contains("/points/delete"));
}

#[test]
fn clear_project_vectors_does_not_touch_memory_vector_collections() {
    let (qdrant_url, handle) = spawn_http_responses(vec![
        (
            200,
            json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}}),
        ),
        (200, json!({"result": {"count": 3}})),
        (200, json!({"result": {"operation_id": 1}})),
    ]);
    let mut lifecycle = CodeSymbolVectorLifecycle::new(
        "project-1".to_string(),
        QdrantConfig {
            url: Some(qdrant_url),
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
            vector_dim: Some(3),
        },
    )
    .expect("lifecycle");

    let cleared = lifecycle.clear_project_vectors().expect("clear vectors");
    let requests = handle.join().expect("qdrant requests");

    assert_eq!(cleared.delete_operations_issued, 3);
    assert_eq!(requests.len(), 3);
    assert!(requests[0].contains("GET /collections/code_symbols_project-1 HTTP/1.1"));
    assert!(requests[1].contains("POST /collections/code_symbols_project-1/points/count HTTP/1.1"));
    assert!(
        requests[2]
            .contains("POST /collections/code_symbols_project-1/points/delete?wait=true HTTP/1.1")
    );
    assert!(
        requests[1..]
            .iter()
            .all(|request| request.contains(r#""key":"project_id""#))
    );
    assert!(
        requests[1..]
            .iter()
            .all(|request| request.contains(r#""value":"project-1""#))
    );
    assert!(!requests[1].contains(r#""key":"file_path""#));
    assert!(!requests[2].contains(r#""key":"file_path""#));
    assert!(requests.iter().all(|request| !request.contains("memory")));
    assert!(
        requests
            .iter()
            .all(|request| !request.contains("GET /collections HTTP/1.1"))
    );
    assert!(
        requests
            .iter()
            .all(|request| !request.contains("DELETE /collections/"))
    );
}

#[test]
fn delete_prefixed_collections_deletes_only_code_symbol_collections() {
    let (qdrant_url, handle) = spawn_http_responses(vec![
        (
            200,
            json!({
                "result": {
                    "collections": [
                        {"name": "code_symbols_project-1"},
                        {"name": "memory_vectors"},
                        {"name": "code_symbols_project-2"}
                    ]
                }
            }),
        ),
        (200, json!({"result": true})),
        (200, json!({"result": true})),
    ]);
    let deleted = delete_code_symbol_collections_with_prefix(&QdrantConfig {
        url: Some(qdrant_url),
        api_key: None,
    })
    .expect("delete prefixed collections");
    let requests = handle.join().expect("qdrant requests");

    assert_eq!(
        deleted,
        vec![
            "code_symbols_project-1".to_string(),
            "code_symbols_project-2".to_string()
        ]
    );
    assert_eq!(requests.len(), 3);
    assert!(requests[0].contains("GET /collections HTTP/1.1"));
    assert!(requests[1].contains("DELETE /collections/code_symbols_project-1 HTTP/1.1"));
    assert!(requests[2].contains("DELETE /collections/code_symbols_project-2 HTTP/1.1"));
    assert!(
        requests
            .iter()
            .all(|request| !request.contains("DELETE /collections/memory_vectors"))
    );
}

use super::*;
use serde_json::json;

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
        CodeVectorSettings::with_vector_dim(Some(3)),
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

#[test]
fn global_prune_collection_discovery_lists_without_deleting() {
    let (qdrant_url, handle) = spawn_http_responses(vec![(
        200,
        json!({
            "result": {
                "collections": [
                    {"name": "code_symbols_11111111-1111-1111-1111-111111111111"},
                    {"name": "code_symbols_invalid"},
                    {"name": "memory_vectors"}
                ]
            }
        }),
    )]);

    let collections = list_code_symbol_collections(&QdrantConfig {
        url: Some(qdrant_url),
        api_key: None,
    })
    .expect("list code-symbol collections");
    let requests = handle.join().expect("qdrant requests");

    assert_eq!(
        collections,
        vec![
            "code_symbols_11111111-1111-1111-1111-111111111111".to_string(),
            "code_symbols_invalid".to_string(),
        ]
    );
    assert_eq!(requests.len(), 1);
    assert!(requests[0].contains("GET /collections HTTP/1.1"));
    assert!(!requests[0].contains("DELETE "));
}

#[test]
fn drop_project_collection_deletes_the_collection_without_a_schema_probe() {
    let (qdrant_url, handle) = spawn_http_responses(vec![(200, json!({"result": true}))]);
    let lifecycle = CodeSymbolVectorLifecycle::new(
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
        CodeVectorSettings::with_vector_dim(Some(3)),
    )
    .expect("lifecycle");

    let dropped = lifecycle
        .drop_project_collection()
        .expect("drop collection");
    let requests = handle.join().expect("qdrant requests");

    assert_eq!(dropped.action, CodeSymbolVectorLifecycleAction::Clear);
    assert_eq!(dropped.collection, "code_symbols_project-1");
    assert_eq!(dropped.delete_operations_issued, 1);
    assert_eq!(requests.len(), 1);
    assert!(requests[0].contains("DELETE /collections/code_symbols_project-1 HTTP/1.1"));
}

#[test]
fn global_prune_collection_delete_404_is_already_missing() {
    let project_id = "11111111-1111-1111-1111-111111111111";
    let (qdrant_url, handle) = spawn_http_responses(vec![(404, json!({"status": "not found"}))]);

    let deleted = delete_project_collection(
        &QdrantConfig {
            url: Some(qdrant_url),
            api_key: None,
        },
        project_id,
    )
    .expect("404 is idempotent");
    let requests = handle.join().expect("qdrant requests");

    assert_eq!(deleted, 0);
    assert_eq!(requests.len(), 1);
    assert!(requests[0].contains(&format!(
        "DELETE /collections/code_symbols_{project_id} HTTP/1.1"
    )));
}

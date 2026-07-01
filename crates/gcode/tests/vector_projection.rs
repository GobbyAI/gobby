mod common;

use common::http::spawn_http_responses;
use gobby_code::config::{CodeVectorSettings, EmbeddingConfig, QdrantConfig};
use gobby_code::models::Symbol;
use gobby_code::vector::code_symbols::{
    CodeSymbolVectorLifecycle, VECTOR_DISTANCE_COSINE, VectorLifecycleError,
};
use serde_json::json;

fn symbol(id: &str, file_path: &str, summary: Option<&str>) -> Symbol {
    Symbol {
        id: id.to_string(),
        project_id: "project-1".to_string(),
        file_path: file_path.to_string(),
        name: "run".to_string(),
        qualified_name: "crate::run".to_string(),
        kind: "function".to_string(),
        language: "rust".to_string(),
        byte_start: 10,
        byte_end: 40,
        line_start: 3,
        line_end: 5,
        signature: Some("fn run()".to_string()),
        docstring: None,
        parent_symbol_id: None,
        content_hash: "hash".to_string(),
        summary: summary.map(str::to_string),
        created_at: String::new(),
        updated_at: String::new(),
    }
}

#[test]
fn ensure_creates_missing_and_reuses_compatible() {
    let (embedding_url, embedding_handle) = spawn_http_responses(vec![
        (200, json!({"data": [{"embedding": [0.1, 0.2, 0.3]}]})),
        (200, json!({"data": [{"embedding": [0.4, 0.5, 0.6]}]})),
    ]);
    let (qdrant_url, qdrant_handle) = spawn_http_responses(vec![
        (404, json!({"status": "not found"})),
        (200, json!({"result": true})),
        (
            200,
            json!({"result": {"operation_id": 1, "status": "completed"}}),
        ),
        (200, json!({"result": {"count": 1}})),
        (200, json!({"result": {"operation_id": 2}})),
        (
            200,
            json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": VECTOR_DISTANCE_COSINE}}}}}),
        ),
        (
            200,
            json!({"result": {"operation_id": 3, "status": "completed"}}),
        ),
        (200, json!({"result": {"count": 1}})),
        (200, json!({"result": {"operation_id": 4}})),
    ]);
    let mut lifecycle = CodeSymbolVectorLifecycle::new(
        "project-1".to_string(),
        QdrantConfig {
            url: Some(qdrant_url),
            api_key: Some("qdrant-key".to_string()),
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

    let first = lifecycle
        .sync_file_symbols("src/lib.rs", &[symbol("sym-1", "src/lib.rs", None)])
        .expect("first sync");
    let second = lifecycle
        .sync_file_symbols(
            "src/lib.rs",
            &[symbol("sym-1", "src/lib.rs", Some("summary"))],
        )
        .expect("second sync");
    let embedding_requests = embedding_handle
        .join()
        .expect("embedding requests")
        .expect("embedding server");
    let qdrant_requests = qdrant_handle
        .join()
        .expect("qdrant requests")
        .expect("qdrant server");

    assert_eq!(first.vectors_upserted, 1);
    assert_eq!(second.vectors_upserted, 1);
    assert_eq!(embedding_requests.len(), 2);
    assert!(qdrant_requests[0].contains("GET /collections/code_symbols_project-1 HTTP/1.1"));
    assert!(qdrant_requests[1].contains("PUT /collections/code_symbols_project-1 HTTP/1.1"));
    assert!(qdrant_requests[1].contains(r#""size":3"#));
    assert!(qdrant_requests[1].contains(r#""distance":"Cosine""#));
    assert!(
        qdrant_requests[2]
            .contains("PUT /collections/code_symbols_project-1/points?wait=true HTTP/1.1")
    );
    assert!(qdrant_requests[2].contains(r#""provenance":"EXTRACTED""#));
    assert!(qdrant_requests[2].contains(r#""source_system":"gcode""#));
    assert!(qdrant_requests[2].contains(r#""source_line_start":3"#));
    assert!(qdrant_requests[2].contains(r#""source_byte_end":40"#));
    assert!(
        qdrant_requests[3]
            .contains("POST /collections/code_symbols_project-1/points/count HTTP/1.1")
    );
    assert!(
        qdrant_requests[4]
            .contains("POST /collections/code_symbols_project-1/points/delete?wait=true HTTP/1.1")
    );
    assert!(qdrant_requests[3].contains(r#""key":"project_id""#));
    assert!(qdrant_requests[3].contains(r#""value":"project-1""#));
    assert!(qdrant_requests[3].contains(r#""key":"file_path""#));
    assert!(qdrant_requests[3].contains(r#""value":"src/lib.rs""#));
    assert!(qdrant_requests[3].contains(r#""must_not""#));
    assert!(qdrant_requests[3].contains(r#""has_id":["sym-1"]"#));
    assert!(qdrant_requests[5].contains("GET /collections/code_symbols_project-1 HTTP/1.1"));
    assert!(!qdrant_requests[5].contains("DELETE"));
}

#[test]
fn clear_and_rebuild_delete_project_and_upsert_current_symbols() {
    let (embedding_url, embedding_handle) = spawn_http_responses(vec![(
        200,
        json!({"data": [{"embedding": [0.7, 0.8, 0.9]}]}),
    )]);
    let (qdrant_url, qdrant_handle) = spawn_http_responses(vec![
        (
            200,
            json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": VECTOR_DISTANCE_COSINE}}}}}),
        ),
        (200, json!({"result": {"count": 1}})),
        (200, json!({"result": {"operation_id": 1}})),
        (
            200,
            json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": VECTOR_DISTANCE_COSINE}}}}}),
        ),
        (
            200,
            json!({"result": {"operation_id": 2, "status": "completed"}}),
        ),
        (200, json!({"result": {"count": 1}})),
        (200, json!({"result": {"operation_id": 3}})),
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

    let cleared = lifecycle.clear_project_vectors().expect("clear");
    let rebuilt = lifecycle
        .rebuild_symbols(&[symbol("sym-1", "src/lib.rs", None)])
        .expect("rebuild");
    let embedding_requests = embedding_handle
        .join()
        .expect("embedding requests")
        .expect("embedding server");
    let qdrant_requests = qdrant_handle
        .join()
        .expect("qdrant requests")
        .expect("qdrant server");

    assert_eq!(cleared.delete_operations_issued, 1);
    assert_eq!(rebuilt.vectors_upserted, 1);
    assert_eq!(embedding_requests.len(), 1);
    assert!(
        qdrant_requests[1]
            .contains("POST /collections/code_symbols_project-1/points/count HTTP/1.1")
    );
    assert!(qdrant_requests[1].contains(r#""key":"project_id""#));
    assert!(!qdrant_requests[1].contains(r#""key":"file_path""#));
    assert!(
        qdrant_requests[2]
            .contains("POST /collections/code_symbols_project-1/points/delete?wait=true HTTP/1.1")
    );
    assert!(
        qdrant_requests[4]
            .contains("PUT /collections/code_symbols_project-1/points?wait=true HTTP/1.1")
    );
    assert!(
        qdrant_requests[5]
            .contains("POST /collections/code_symbols_project-1/points/count HTTP/1.1")
    );
    assert!(
        qdrant_requests[6]
            .contains("POST /collections/code_symbols_project-1/points/delete?wait=true HTTP/1.1")
    );
    assert!(qdrant_requests[5].contains(r#""must_not""#));
    assert!(qdrant_requests[5].contains(r#""has_id":["sym-1"]"#));
}

#[test]
fn incompatible_existing_collection_errors_without_migration() {
    let (qdrant_url, qdrant_handle) = spawn_http_responses(vec![
        (
            200,
            json!({"result": {"config": {"params": {"vectors": {"size": 4, "distance": "Dot"}}}}}),
        ),
        (
            200,
            json!({"result": {"config": {"params": {"vectors": {"size": 4, "distance": "Dot"}}}}}),
        ),
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

    let err = lifecycle
        .ensure_collection()
        .expect_err("incompatible ensure must fail");
    assert!(matches!(
        err,
        VectorLifecycleError::DimensionMismatch {
            expected_size: 3,
            found_size: Some(4),
            expected_distance: VECTOR_DISTANCE_COSINE,
            found_distance: Some(ref distance),
            ..
        } if distance == "Dot"
    ));

    let err = lifecycle
        .clear_project_vectors()
        .expect_err("incompatible clear must fail before delete");
    assert!(matches!(
        err,
        VectorLifecycleError::DimensionMismatch { .. }
    ));
    let qdrant_requests = qdrant_handle
        .join()
        .expect("qdrant requests")
        .expect("qdrant server");

    assert_eq!(qdrant_requests.len(), 2);
    assert!(qdrant_requests.iter().all(|request| {
        request.contains("GET /collections/code_symbols_project-1 HTTP/1.1")
            && !request.contains("/points/delete")
            && !request.contains("/points HTTP/1.1")
    }));
}

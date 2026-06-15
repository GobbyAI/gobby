use super::*;
use serde_json::json;

#[test]
fn collection_name_is_stable() {
    assert_eq!(
        collection_name(CODE_SYMBOL_COLLECTION_PREFIX, "project-1").unwrap(),
        "code_symbols_project-1"
    );
}

#[test]
fn collection_name_rejects_invalid_custom_project_ids() {
    let error = collection_name(CODE_SYMBOL_COLLECTION_PREFIX, "bad/project")
        .expect_err("path-like custom collection should fail");

    assert!(error.to_string().contains("invalid character"));
}

#[test]
fn search_code_symbols_reports_missing_qdrant_config() {
    let request = CodeSymbolVectorSearchRequest {
        project_id: "project-1".to_string(),
        query: "run".to_string(),
        limit: 10,
        collection_prefix: CODE_SYMBOL_COLLECTION_PREFIX.to_string(),
    };

    let error = search_code_symbols(&test_context(None), &request)
        .expect_err("missing Qdrant config should be surfaced");

    assert_eq!(error, SearchError::MissingQdrantConfig);
}

#[test]
fn delete_project_collection_targets_only_project_collection() {
    let (qdrant_url, handle) = spawn_http_responses(vec![(200, json!({"result": true}))]);
    let deleted = delete_project_collection(
        &QdrantConfig {
            url: Some(qdrant_url),
            api_key: Some("qdrant-key".to_string()),
        },
        "project-1",
    )
    .expect("delete collection");
    let requests = handle.join().expect("qdrant requests");

    assert_eq!(deleted, 1);
    assert_eq!(requests.len(), 1);
    assert!(requests[0].contains("DELETE /collections/code_symbols_project-1 HTTP/1.1"));
    assert!(requests[0].contains("api-key: qdrant-key"));
    assert!(!requests[0].contains("project-2"));
}

#[test]
fn blank_qdrant_url_is_missing_config() {
    let err = delete_project_collection(
        &QdrantConfig {
            url: Some(" \t ".to_string()),
            api_key: None,
        },
        "project-1",
    )
    .expect_err("blank URL should be treated as missing config");

    assert_eq!(err, VectorLifecycleError::MissingQdrantConfig);
}

#[test]
fn delete_project_collection_rejects_invalid_collection_name() {
    let err = delete_project_collection(
        &QdrantConfig {
            url: Some("http://127.0.0.1:1".to_string()),
            api_key: None,
        },
        "bad/project",
    )
    .expect_err("invalid collection should fail before delete request");

    assert!(matches!(
        err,
        VectorLifecycleError::InvalidCollectionName(_)
    ));
}

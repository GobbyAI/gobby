use super::*;
use crate::config::QdrantConfig;
use crate::degradation::ServiceState;
use crate::test_http::{RequestHandle, read_http_request, spawn_json_response_with_status};
use serde_json::{Map, Value, json};
use std::io::{self, Write};
use std::net::TcpListener;
use std::thread;
use std::time::Duration;

#[test]
fn payload_schema_is_opaque() {
    let mut payload = Map::new();
    payload.insert("symbol_id".to_string(), json!("sym-1"));
    payload.insert("wiki".to_string(), json!({"topic": "rust"}));

    let upsert = UpsertRequest {
        id: "point-1".to_string(),
        vector: vec![0.25, 0.5],
        payload: payload.clone(),
    };
    let search = SearchRequest {
        vector: vec![0.25, 0.5],
        limit: 5,
        filter: Some(json!({"must": [{"key": "kind", "match": {"value": "fn"}}]})),
    };

    assert_eq!(upsert.payload, payload);
    assert_eq!(search.filter.as_ref().unwrap()["must"][0]["key"], "kind");
}

#[test]
fn with_qdrant_degradation_contract() {
    let config = QdrantConfig {
        url: Some("http://localhost:6333".to_string()),
        api_key: None,
    };
    let missing_url = QdrantConfig {
        url: None,
        api_key: None,
    };

    assert_eq!(
        with_qdrant(None, vec!["default"], |_| Ok(vec!["value"])).unwrap(),
        (vec!["default"], ServiceState::NotConfigured)
    );
    assert_eq!(
        with_qdrant(Some(&missing_url), 7, |_| Ok(9)).unwrap(),
        (7, ServiceState::NotConfigured)
    );
    assert_eq!(
        with_qdrant(Some(&config), "default", |_| Ok("value")).unwrap(),
        ("value", ServiceState::Available)
    );

    let err = with_qdrant(Some(&config), 0, |_| anyhow::bail!("qdrant failed"))
        .expect_err("closure errors propagate");
    assert_eq!(err.to_string(), "qdrant failed");
}

#[test]
fn sync_search_from_cli_path() {
    let (base_url, request_handle) = spawn_qdrant_response(
        200,
        json!({
            "result": [
                {
                    "id": "point-1",
                    "score": 0.93,
                    "payload": {"symbol_id": "sym-1", "kind": "function"}
                }
            ]
        }),
    );
    let config = QdrantConfig {
        url: Some(base_url),
        api_key: Some("secret-key".to_string()),
    };

    let hits = search(
        &config,
        "code_symbols_project",
        SearchRequest {
            vector: vec![0.1, 0.2],
            limit: 3,
            filter: Some(json!({"must": []})),
        },
    )
    .expect("search succeeds");
    let request = request_handle.join().expect("request thread").unwrap();

    assert_eq!(hits.len(), 1);
    assert_eq!(hits[0].id, "point-1");
    assert_eq!(hits[0].score, 0.93);
    assert_eq!(hits[0].payload["symbol_id"], "sym-1");
    assert!(request.contains("POST /collections/code_symbols_project/points/search HTTP/1.1"));
    assert!(request.contains("api-key: secret-key"));
    assert!(request.contains(r#""with_payload":true"#));
}

#[test]
fn with_qdrant_search_composition() {
    let (base_url, request_handle) = spawn_qdrant_response(
        200,
        json!({"result": [{"id": "point-1", "score": 0.5, "payload": {}}]}),
    );
    let config = QdrantConfig {
        url: Some(base_url),
        api_key: None,
    };

    let (hits, state) = with_qdrant(Some(&config), vec![], |cfg| {
        search(
            cfg,
            "collection",
            SearchRequest {
                vector: vec![0.1],
                limit: 1,
                filter: None,
            },
        )
    })
    .expect("composed search");
    request_handle.join().expect("request thread").unwrap();

    assert_eq!(state, ServiceState::Available);
    assert_eq!(hits[0].id, "point-1");
}

#[test]
fn upsert_requires_completed_qdrant_operation() {
    let (base_url, request_handle) = spawn_qdrant_response(
        200,
        json!({"result": {"operation_id": 17, "status": "completed"}, "status": "ok"}),
    );
    let config = QdrantConfig {
        url: Some(base_url),
        api_key: None,
    };

    let result = upsert(
        &config,
        "collection",
        vec![UpsertRequest {
            id: "point-1".to_string(),
            vector: vec![0.1],
            payload: Map::new(),
        }],
    )
    .expect("completed upsert succeeds");
    let request = request_handle.join().expect("request thread").unwrap();

    assert_eq!(
        result,
        UpsertResult {
            operation_id: Some(17),
            status: "completed".to_string()
        }
    );
    assert!(request.contains("PUT /collections/collection/points?wait=true HTTP/1.1"));
}

#[test]
fn upsert_batched_splits_points_by_batch_size() {
    let responses = vec![
        (
            200,
            json!({"result": {"operation_id": 17, "status": "completed"}, "status": "ok"}),
        ),
        (
            200,
            json!({"result": {"operation_id": 18, "status": "completed"}, "status": "ok"}),
        ),
        (
            200,
            json!({"result": {"operation_id": 19, "status": "completed"}, "status": "ok"}),
        ),
    ];
    let (base_url, request_handle) = spawn_qdrant_responses(responses);
    let config = QdrantConfig {
        url: Some(base_url),
        api_key: None,
    };
    let points = (0..5)
        .map(|index| UpsertRequest {
            id: format!("point-{index}"),
            vector: vec![index as f32],
            payload: Map::new(),
        })
        .collect();

    let upserted =
        upsert_batched_with_size(&config, "collection", points, 2).expect("batched upsert");
    let requests = request_handle
        .join()
        .expect("request thread")
        .expect("requests");

    assert_eq!(upserted, 5);
    assert_eq!(requests.len(), 3);
    assert!(requests[0].contains(r#""point-0""#));
    assert!(requests[0].contains(r#""point-1""#));
    assert!(!requests[0].contains(r#""point-2""#));
    assert!(requests[1].contains(r#""point-2""#));
    assert!(requests[1].contains(r#""point-3""#));
    assert!(requests[2].contains(r#""point-4""#));
}

#[test]
fn upsert_rejects_non_completed_qdrant_operation() {
    let (base_url, request_handle) = spawn_qdrant_response(
        200,
        json!({"result": {"operation_id": 18, "status": "acknowledged"}, "status": "ok"}),
    );
    let config = QdrantConfig {
        url: Some(base_url),
        api_key: None,
    };

    let err = upsert(
        &config,
        "collection",
        vec![UpsertRequest {
            id: "point-1".to_string(),
            vector: vec![0.1],
            payload: Map::new(),
        }],
    )
    .expect_err("acknowledged upsert is not complete");
    request_handle.join().expect("request thread").unwrap();

    match err.downcast_ref::<QdrantError>() {
        Some(QdrantError::OperationStatus {
            operation,
            operation_status,
            collection,
            request,
        }) => {
            assert_eq!(*operation, "upsert");
            assert_eq!(operation_status, "acknowledged");
            assert_eq!(collection.as_deref(), Some("collection"));
            assert_eq!(
                request.as_deref(),
                Some("PUT /collections/collection/points?wait=true")
            );
        }
        Some(other) => panic!("expected QdrantError::OperationStatus, got {other:?}"),
        None => panic!("expected QdrantError::OperationStatus, got {err}"),
    }
}

#[test]
fn qdrant_single_state_boundary() {
    let missing_url = QdrantConfig {
        url: None,
        api_key: None,
    };
    let (default_hits, state) = with_qdrant(Some(&missing_url), Vec::<SearchHit>::new(), |_| {
        unreachable!("search should not run without qdrant url")
    })
    .expect("missing url degrades");
    assert_eq!(default_hits.len(), 0);
    assert_eq!(state, ServiceState::NotConfigured);

    let (base_url, request_handle) =
        spawn_qdrant_response(503, json!({"status": "service unavailable"}));
    let config = QdrantConfig {
        url: Some(base_url),
        api_key: None,
    };
    let (hits, state) = with_qdrant(Some(&config), Vec::<SearchHit>::new(), |cfg| {
        search(
            cfg,
            "collection",
            SearchRequest {
                vector: vec![0.1],
                limit: 1,
                filter: None,
            },
        )
    })
    .expect("http errors degrade out of qdrant boundary");
    request_handle.join().expect("request thread").unwrap();

    assert!(hits.is_empty());
    assert!(matches!(
        state,
        ServiceState::Unreachable { ref message }
            if message.contains("Qdrant search failed for collection `collection`")
                && message.contains("HTTP 503")
    ));
}

#[test]
fn qdrant_http_failures_are_typed_errors() {
    let (search_url, search_handle) =
        spawn_qdrant_response(503, json!({"status": "service unavailable"}));
    let search_config = QdrantConfig {
        url: Some(search_url),
        api_key: None,
    };
    let err = search(
        &search_config,
        "collection",
        SearchRequest {
            vector: vec![0.1],
            limit: 1,
            filter: None,
        },
    )
    .expect_err("search HTTP failure is typed");
    search_handle
        .join()
        .expect("search request thread")
        .unwrap();
    match err.downcast_ref::<QdrantError>() {
        Some(QdrantError::HttpStatus {
            operation,
            status,
            body,
            collection,
            request,
        }) => {
            assert_eq!(*operation, "search");
            assert_eq!(*status, reqwest::StatusCode::SERVICE_UNAVAILABLE);
            assert!(body.contains("service unavailable"));
            assert_eq!(collection.as_deref(), Some("collection"));
            assert_eq!(
                request.as_deref(),
                Some("POST /collections/collection/points/search")
            );
        }
        Some(other) => panic!("expected QdrantError::HttpStatus, got {other:?}"),
        None => panic!("expected QdrantError, got {err}"),
    }

    let (upsert_url, upsert_handle) = spawn_qdrant_response(500, json!({"status": "boom"}));
    let upsert_config = QdrantConfig {
        url: Some(upsert_url),
        api_key: None,
    };
    let err = upsert(
        &upsert_config,
        "collection",
        vec![UpsertRequest {
            id: "point-1".to_string(),
            vector: vec![0.1],
            payload: Map::new(),
        }],
    )
    .expect_err("upsert HTTP failure is typed");
    upsert_handle
        .join()
        .expect("upsert request thread")
        .unwrap();
    match err.downcast_ref::<QdrantError>() {
        Some(QdrantError::HttpStatus {
            operation,
            status,
            body,
            collection,
            request,
        }) => {
            assert_eq!(*operation, "upsert");
            assert_eq!(*status, reqwest::StatusCode::INTERNAL_SERVER_ERROR);
            assert!(body.contains("boom"));
            assert_eq!(collection.as_deref(), Some("collection"));
            assert_eq!(
                request.as_deref(),
                Some("PUT /collections/collection/points?wait=true")
            );
        }
        Some(other) => panic!("expected QdrantError::HttpStatus, got {other:?}"),
        None => panic!("expected QdrantError, got {err}"),
    }
}

#[test]
fn qdrant_http_status_unreachable_only_for_server_errors() {
    let client_error = anyhow::Error::new(QdrantError::HttpStatus {
        operation: "search",
        status: reqwest::StatusCode::BAD_REQUEST,
        body: "bad request".to_string(),
        collection: None,
        request: None,
    });
    let server_error = anyhow::Error::new(QdrantError::HttpStatus {
        operation: "search",
        status: reqwest::StatusCode::INTERNAL_SERVER_ERROR,
        body: "boom".to_string(),
        collection: None,
        request: None,
    });

    assert!(!is_qdrant_unreachable(&client_error));
    assert!(is_qdrant_unreachable(&server_error));
}

#[test]
fn qdrant_collection_schema_rejects_named_or_unrecognized_vectors() {
    let expected = VectorCollectionSchema {
        size: 3,
        distance: "Cosine".to_string(),
    };
    for data in [
        json!({"result":{"config":{"params":{"vectors":{"default":{"size":3,"distance":"Cosine"}}}}}}),
        json!({"result":{"config":{"params":{}}}}),
    ] {
        let found = parse_collection_schema(&data);
        let error = ensure_compatible_collection("test", &expected, &found)
            .expect_err("incomplete schema should be rejected");
        assert!(error.to_string().contains("incompatible schema"));
    }
}

#[test]
fn collection_lifecycle_ensures_schema_and_deletes_filtered_points() {
    let (schema_url, schema_handle) = spawn_qdrant_response(
        200,
        json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}}),
    );
    let config = QdrantConfig {
        url: Some(schema_url),
        api_key: Some("secret".to_string()),
    };

    let schema = ensure_collection(
        &config,
        "gwiki_project_project-1",
        VectorCollectionSchema {
            size: 3,
            distance: "Cosine".to_string(),
        },
    )
    .expect("existing collection should be reused");
    let request = schema_handle
        .join()
        .expect("schema request thread")
        .unwrap();
    assert!(request.starts_with("GET /collections/gwiki_project_project-1 HTTP/1.1"));
    assert!(request.contains("api-key: secret"));
    assert_eq!(schema.size, 3);
    assert_eq!(schema.distance, "Cosine");

    let filter = json!({
        "must": [
            {"key": "namespace", "match": {"value": "gwiki"}},
            {"key": "scope_kind", "match": {"value": "project"}},
            {"key": "project_id", "match": {"value": "project-1"}},
            {"key": "path", "match": {"value": "notes/page.md"}}
        ]
    });
    let (delete_url, delete_handle) = spawn_qdrant_response(
        200,
        json!({"status": "ok", "result": {"status": "completed"}}),
    );
    let config = QdrantConfig {
        url: Some(delete_url),
        api_key: None,
    };

    delete_points_by_filter(&config, "gwiki_project_project-1", filter).expect("delete by filter");
    let request = delete_handle
        .join()
        .expect("delete request thread")
        .unwrap();
    assert!(
        request.starts_with(
            "POST /collections/gwiki_project_project-1/points/delete?wait=true HTTP/1.1"
        )
    );
    assert!(request.contains("\"path\""));
    assert!(request.contains("\"notes/page.md\""));

    for body in [
        json!({"status": "ok"}),
        json!({"status": "ok", "result": {}}),
        json!({"status": "ok", "result": {"status": 7}}),
    ] {
        let (delete_url, delete_handle) = spawn_qdrant_response(200, body);
        let config = QdrantConfig {
            url: Some(delete_url),
            api_key: None,
        };
        let error =
            delete_points_by_filter(&config, "gwiki_project_project-1", json!({"must": []}))
                .expect_err("malformed delete operation status should fail");
        assert!(error.downcast_ref::<QdrantError>().is_some());
        delete_handle
            .join()
            .expect("delete request thread")
            .unwrap();
    }
}

#[test]
fn collection_point_count_reads_collection_info() {
    let (base_url, request_handle) = spawn_qdrant_response(
        200,
        json!({"result": {"points_count": 3, "vectors_count": 9}}),
    );
    let config = QdrantConfig {
        url: Some(base_url),
        api_key: None,
    };

    let count = collection_point_count(&config, "gwiki_project_project-1")
        .expect("collection count")
        .expect("points count");
    let request = request_handle.join().expect("request thread").unwrap();

    assert_eq!(count, 3);
    assert!(request.starts_with("GET /collections/gwiki_project_project-1 HTTP/1.1"));

    assert_eq!(
        parse_collection_point_count(&json!({"result": {"vectors_count": 4}})),
        Some(4)
    );
    assert_eq!(
        parse_collection_point_count(&json!({"result": {"points_count": "unknown"}})),
        None
    );
}

fn spawn_qdrant_response(status: u16, body: Value) -> (String, RequestHandle) {
    spawn_json_response_with_status(status, body.to_string()).expect("spawn qdrant test server")
}

fn spawn_qdrant_responses(
    responses: Vec<(u16, Value)>,
) -> (String, thread::JoinHandle<io::Result<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind qdrant test server");
    let base_url = format!("http://{}", listener.local_addr().expect("local addr"));
    let handle = thread::spawn(move || {
        let mut requests = Vec::new();
        for (status, body) in responses {
            let (mut stream, _) = listener.accept()?;
            stream.set_read_timeout(Some(Duration::from_secs(2)))?;
            requests.push(read_http_request(&mut stream)?);

            let reason = StatusCode::from_u16(status)
                .ok()
                .and_then(|status| status.canonical_reason())
                .unwrap_or("OK");
            let body = body.to_string();
            write!(
                stream,
                "HTTP/1.1 {status} {reason}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                body.len()
            )?;
        }
        Ok(requests)
    });

    (base_url, handle)
}

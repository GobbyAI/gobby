//! Qdrant foundation adapter boundary.
//!
//! This module is available with the `qdrant` feature. Consumers should surface
//! missing or unreachable vector services as typed degradation unless a command
//! explicitly requires semantic search.

use crate::config::QdrantConfig;
use crate::degradation::ServiceState;
use serde_json::{Map, Value};
use std::time::Duration;

const QDRANT_TIMEOUT: Duration = Duration::from_secs(5);
pub use reqwest::StatusCode;

#[derive(Debug, thiserror::Error)]
pub enum QdrantError {
    #[error("Qdrant {operation} failed{context}: HTTP {status}: {body}", context = http_status_context(collection, request))]
    HttpStatus {
        operation: &'static str,
        status: reqwest::StatusCode,
        body: String,
        collection: Option<String>,
        request: Option<String>,
    },
    #[error("Qdrant {operation} failed{context}: operation status `{operation_status}`", context = http_status_context(collection, request))]
    OperationStatus {
        operation: &'static str,
        operation_status: String,
        collection: Option<String>,
        request: Option<String>,
    },
}

fn http_status_context(collection: &Option<String>, request: &Option<String>) -> String {
    match (collection.as_deref(), request.as_deref()) {
        (Some(collection), Some(request)) => {
            format!(" for collection `{collection}` during `{request}`")
        }
        (Some(collection), None) => format!(" for collection `{collection}`"),
        (None, Some(request)) => format!(" during `{request}`"),
        (None, None) => String::new(),
    }
}

/// Scope for a Qdrant collection, allowing caller-controlled naming.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CollectionScope<'a> {
    /// `{namespace}_project_{id}` — per-project vector store.
    Project(&'a str),
    /// `{namespace}_topic_{name}` — topic-scoped store.
    Topic(&'a str),
    /// Verbatim collection name, without namespace prefixing.
    Custom(&'a str),
}

/// Build a collection name from namespace and scope.
pub fn collection_name(namespace: &str, scope: CollectionScope<'_>) -> String {
    match scope {
        CollectionScope::Project(id) => format!("{namespace}_project_{id}"),
        CollectionScope::Topic(name) => format!("{namespace}_topic_{name}"),
        CollectionScope::Custom(name) => name.to_string(),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VectorCollectionSchema {
    pub size: usize,
    pub distance: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExistingVectorCollectionSchema {
    pub size: Option<usize>,
    pub distance: Option<String>,
}

/// Vector upsert request with opaque domain payload.
#[derive(Debug, Clone, PartialEq)]
pub struct UpsertRequest {
    pub id: String,
    pub vector: Vec<f32>,
    pub payload: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpsertResult {
    pub operation_id: Option<u64>,
    pub status: String,
}

/// Vector search request with opaque domain filter.
#[derive(Debug, Clone, PartialEq)]
pub struct SearchRequest {
    pub vector: Vec<f32>,
    pub limit: usize,
    pub filter: Option<Value>,
}

/// Vector search result with score and opaque payload.
#[derive(Debug, Clone, PartialEq)]
pub struct SearchHit {
    pub id: String,
    pub score: f32,
    pub payload: Map<String, Value>,
}

/// Run a closure with Qdrant config, with typed degradation for missing config.
pub fn with_qdrant<T>(
    config: Option<&QdrantConfig>,
    default: T,
    f: impl FnOnce(&QdrantConfig) -> anyhow::Result<T>,
) -> anyhow::Result<(T, ServiceState)> {
    let Some(config) = config else {
        return Ok((default, ServiceState::NotConfigured));
    };
    if config.url.is_none() {
        return Ok((default, ServiceState::NotConfigured));
    }

    match f(config) {
        Ok(value) => Ok((value, ServiceState::Available)),
        Err(error) if is_qdrant_unreachable(&error) => Ok((
            default,
            ServiceState::Unreachable {
                message: error.to_string(),
            },
        )),
        Err(error) => Err(error),
    }
}

/// Execute a vector search via Qdrant REST API.
pub fn search(
    config: &QdrantConfig,
    collection: &str,
    request: SearchRequest,
) -> anyhow::Result<Vec<SearchHit>> {
    let url = config
        .url
        .as_deref()
        .ok_or_else(|| anyhow::anyhow!("Qdrant URL not configured"))?
        .trim_end_matches('/');
    let client = reqwest::blocking::Client::builder()
        .timeout(QDRANT_TIMEOUT)
        .build()?;

    let collection_path = encoded_collection(collection);
    let request_path = format!("/collections/{collection_path}/points/search");
    let mut req = client.post(format!("{url}{request_path}"));
    if let Some(key) = &config.api_key {
        req = req.header("api-key", key);
    }

    let body = serde_json::json!({
        "vector": request.vector,
        "limit": request.limit,
        "filter": request.filter,
        "with_payload": true,
    });
    let resp = req.json(&body).send()?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp
            .text()
            .unwrap_or_else(|err| format!("<failed to read response body: {err}>"));
        return Err(QdrantError::HttpStatus {
            operation: "search",
            status,
            body,
            collection: Some(collection.to_string()),
            request: Some(format!("POST {request_path}")),
        }
        .into());
    }

    let data: Value = resp.json()?;
    let hits = data
        .get("result")
        .and_then(Value::as_array)
        .map(|results| {
            results
                .iter()
                .filter_map(parse_search_hit)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();

    Ok(hits)
}

/// Ensure a Qdrant collection exists with the expected vector schema.
pub fn ensure_collection(
    config: &QdrantConfig,
    collection: &str,
    expected: VectorCollectionSchema,
) -> anyhow::Result<VectorCollectionSchema> {
    match collection_schema(config, collection)? {
        Some(found) => {
            ensure_compatible_collection(collection, &expected, &found)?;
            Ok(VectorCollectionSchema {
                size: found.size.unwrap_or(expected.size),
                distance: found.distance.unwrap_or_else(|| expected.distance.clone()),
            })
        }
        None => {
            create_collection(config, collection, &expected)?;
            Ok(expected)
        }
    }
}

/// Return the collection vector schema, or `None` when the collection is absent.
pub fn collection_schema(
    config: &QdrantConfig,
    collection: &str,
) -> anyhow::Result<Option<ExistingVectorCollectionSchema>> {
    let request_path = collection_request_path(collection);
    let resp = qdrant_request(config, reqwest::Method::GET, &request_path)?.send()?;
    let status = resp.status();
    if status == StatusCode::NOT_FOUND {
        return Ok(None);
    }
    if !status.is_success() {
        return Err(qdrant_http_error(
            "get collection",
            status,
            resp,
            collection,
            request_path,
        ));
    }

    let data: Value = resp.json()?;
    Ok(Some(parse_collection_schema(&data)))
}

/// Delete all points matching a Qdrant filter.
pub fn delete_points_by_filter(
    config: &QdrantConfig,
    collection: &str,
    filter: Value,
) -> anyhow::Result<()> {
    let request_path = format!(
        "{}/points/delete?wait=true",
        collection_request_path(collection)
    );
    let resp = qdrant_request(config, reqwest::Method::POST, &request_path)?
        .json(&serde_json::json!({ "filter": filter }))
        .send()?;
    let status = resp.status();
    if status == StatusCode::NOT_FOUND {
        return Ok(());
    }
    if !status.is_success() {
        return Err(qdrant_http_error(
            "delete points",
            status,
            resp,
            collection,
            request_path,
        ));
    }

    let data: Value = resp.json()?;
    if let Some(result) = data.get("result") {
        let Some(operation_status) = result.get("status").and_then(Value::as_str) else {
            let operation_status = result
                .get("status")
                .map(Value::to_string)
                .unwrap_or_else(|| "<missing>".to_string());
            return Err(QdrantError::OperationStatus {
                operation: "delete points",
                operation_status,
                collection: Some(collection.to_string()),
                request: Some(format!("POST {request_path}")),
            }
            .into());
        };
        if operation_status != "completed" {
            return Err(QdrantError::OperationStatus {
                operation: "delete points",
                operation_status: operation_status.to_string(),
                collection: Some(collection.to_string()),
                request: Some(format!("POST {request_path}")),
            }
            .into());
        }
    }
    Ok(())
}

fn create_collection(
    config: &QdrantConfig,
    collection: &str,
    schema: &VectorCollectionSchema,
) -> anyhow::Result<()> {
    let request_path = collection_request_path(collection);
    let body = serde_json::json!({
        "vectors": {
            "size": schema.size,
            "distance": schema.distance,
        },
    });
    let resp = qdrant_request(config, reqwest::Method::PUT, &request_path)?
        .json(&body)
        .send()?;
    let status = resp.status();
    if !status.is_success() {
        return Err(qdrant_http_error(
            "create collection",
            status,
            resp,
            collection,
            request_path,
        ));
    }
    Ok(())
}

/// Execute a batch vector upsert via Qdrant REST API.
pub fn upsert(
    config: &QdrantConfig,
    collection: &str,
    points: Vec<UpsertRequest>,
) -> anyhow::Result<UpsertResult> {
    let url = config
        .url
        .as_deref()
        .ok_or_else(|| anyhow::anyhow!("Qdrant URL not configured"))?
        .trim_end_matches('/');
    let client = reqwest::blocking::Client::builder()
        .timeout(QDRANT_TIMEOUT)
        .build()?;

    let points: Vec<Value> = points
        .into_iter()
        .map(|point| {
            serde_json::json!({
                "id": point.id,
                "vector": point.vector,
                "payload": point.payload,
            })
        })
        .collect();
    let body = serde_json::json!({ "points": points });

    let collection_path = encoded_collection(collection);
    let request_path = format!("/collections/{collection_path}/points?wait=true");
    let mut req = client.put(format!("{url}{request_path}"));
    if let Some(key) = &config.api_key {
        req = req.header("api-key", key);
    }

    let resp = req.json(&body).send()?;
    let status = resp.status();
    if !status.is_success() {
        let body = resp
            .text()
            .unwrap_or_else(|err| format!("<failed to read response body: {err}>"));
        return Err(QdrantError::HttpStatus {
            operation: "upsert",
            status,
            body,
            collection: Some(collection.to_string()),
            request: Some(format!("PUT {request_path}")),
        }
        .into());
    }

    let data: Value = resp.json()?;
    let result = parse_upsert_result(&data)?;
    if result.status != "completed" {
        return Err(QdrantError::OperationStatus {
            operation: "upsert",
            operation_status: result.status,
            collection: Some(collection.to_string()),
            request: Some(format!("PUT {request_path}")),
        }
        .into());
    }

    Ok(result)
}

fn parse_upsert_result(data: &Value) -> anyhow::Result<UpsertResult> {
    let result = data
        .get("result")
        .ok_or_else(|| anyhow::anyhow!("Qdrant upsert response did not include result"))?;
    let status = result
        .get("status")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow::anyhow!("Qdrant upsert response did not include result.status"))?
        .to_string();
    let operation_id = result.get("operation_id").and_then(Value::as_u64);
    Ok(UpsertResult {
        operation_id,
        status,
    })
}

fn parse_search_hit(hit: &Value) -> Option<SearchHit> {
    let id = parse_point_id(hit.get("id")?)?;
    let score = hit.get("score")?.as_f64()? as f32;
    let payload = hit
        .get("payload")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();

    Some(SearchHit { id, score, payload })
}

fn parse_point_id(id: &Value) -> Option<String> {
    match id {
        Value::String(value) => Some(value.clone()),
        Value::Number(value) => Some(value.to_string()),
        _ => None,
    }
}

fn parse_collection_schema(data: &Value) -> ExistingVectorCollectionSchema {
    let vectors = data.pointer("/result/config/params/vectors");
    let size = vectors
        .and_then(|vectors| vectors.get("size"))
        .and_then(Value::as_u64)
        .and_then(|size| usize::try_from(size).ok());
    let distance = vectors
        .and_then(|vectors| vectors.get("distance"))
        .and_then(Value::as_str)
        .map(str::to_string);
    ExistingVectorCollectionSchema { size, distance }
}

fn ensure_compatible_collection(
    collection: &str,
    expected: &VectorCollectionSchema,
    found: &ExistingVectorCollectionSchema,
) -> anyhow::Result<()> {
    if found.size != Some(expected.size)
        || found.distance.as_deref() != Some(expected.distance.as_str())
    {
        anyhow::bail!(
            "Qdrant collection `{collection}` has incompatible schema: expected size {} distance {}, found size {:?} distance {:?}",
            expected.size,
            expected.distance,
            found.size,
            found.distance
        );
    }
    Ok(())
}

fn is_qdrant_unreachable(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        cause
            .downcast_ref::<reqwest::Error>()
            .is_some_and(|error| error.is_connect() || error.is_timeout())
            || matches!(
                cause.downcast_ref::<QdrantError>(),
                // Qdrant 4xx responses are caller/configuration errors; only 5xx
                // responses represent service-side unavailability.
                Some(QdrantError::HttpStatus { status, .. }) if status.is_server_error()
            )
    })
}

fn encoded_collection(collection: &str) -> String {
    urlencoding::encode(collection).into_owned()
}

fn collection_request_path(collection: &str) -> String {
    format!("/collections/{}", encoded_collection(collection))
}

fn qdrant_request(
    config: &QdrantConfig,
    method: reqwest::Method,
    path: &str,
) -> anyhow::Result<reqwest::blocking::RequestBuilder> {
    let url = config
        .url
        .as_deref()
        .ok_or_else(|| anyhow::anyhow!("Qdrant URL not configured"))?
        .trim_end_matches('/');
    let client = reqwest::blocking::Client::builder()
        .timeout(QDRANT_TIMEOUT)
        .build()?;
    let mut req = client.request(method, format!("{url}{path}"));
    if let Some(key) = &config.api_key {
        req = req.header("api-key", key);
    }
    Ok(req)
}

fn qdrant_http_error(
    operation: &'static str,
    status: StatusCode,
    resp: reqwest::blocking::Response,
    collection: &str,
    request_path: String,
) -> anyhow::Error {
    let body = resp
        .text()
        .unwrap_or_else(|err| format!("<failed to read response body: {err}>"));
    QdrantError::HttpStatus {
        operation,
        status,
        body,
        collection: Some(collection.to_string()),
        request: Some(format!("{} {request_path}", operation_method(operation))),
    }
    .into()
}

fn operation_method(operation: &str) -> &'static str {
    match operation {
        "get collection" => "GET",
        "create collection" => "PUT",
        "delete points" => "POST",
        "search" => "POST",
        "upsert" => "PUT",
        _ => "POST",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::QdrantConfig;
    use crate::degradation::ServiceState;
    use crate::test_http::{RequestHandle, spawn_json_response_with_status};
    use serde_json::{Map, Value, json};

    #[test]
    fn collection_name_covers_all_scopes() {
        assert_eq!(
            collection_name("gwiki", CollectionScope::Project("abc-123")),
            "gwiki_project_abc-123"
        );
        assert_eq!(
            collection_name("gwiki", CollectionScope::Topic("rust-async")),
            "gwiki_topic_rust-async"
        );
        assert_eq!(
            collection_name("gcode", CollectionScope::Custom("code_symbols_abc-123")),
            "code_symbols_abc-123"
        );
    }

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
    fn custom_scope_returns_verbatim_name() {
        assert_eq!(
            collection_name("ignored", CollectionScope::Custom("code_symbols_project-1")),
            "code_symbols_project-1"
        );
    }

    #[test]
    fn qdrant_single_state_boundary() {
        let missing_url = QdrantConfig {
            url: None,
            api_key: None,
        };
        let (default_hits, state) =
            with_qdrant(Some(&missing_url), Vec::<SearchHit>::new(), |_| {
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

        delete_points_by_filter(&config, "gwiki_project_project-1", filter)
            .expect("delete by filter");
        let request = delete_handle
            .join()
            .expect("delete request thread")
            .unwrap();
        assert!(request.starts_with(
            "POST /collections/gwiki_project_project-1/points/delete?wait=true HTTP/1.1"
        ));
        assert!(request.contains("\"path\""));
        assert!(request.contains("\"notes/page.md\""));

        for body in [
            json!({"status": "ok", "result": {}}),
            json!({"status": "ok", "result": {"status": 7}}),
        ] {
            let (delete_url, _delete_handle) = spawn_qdrant_response(200, body);
            let config = QdrantConfig {
                url: Some(delete_url),
                api_key: None,
            };
            let error =
                delete_points_by_filter(&config, "gwiki_project_project-1", json!({"must": []}))
                    .expect_err("malformed delete operation status should fail");
            assert!(error.downcast_ref::<QdrantError>().is_some());
        }
    }

    fn spawn_qdrant_response(status: u16, body: Value) -> (String, RequestHandle) {
        spawn_json_response_with_status(status, body.to_string()).expect("spawn qdrant test server")
    }
}

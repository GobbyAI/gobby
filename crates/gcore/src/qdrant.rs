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
pub const DEFAULT_UPSERT_BATCH_SIZE: usize = 128;
pub use reqwest::StatusCode;

mod naming;
pub use naming::{CollectionNameError, CollectionScope, collection_name};

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

/// Return the current point count for a collection, or `None` when absent or unavailable.
pub fn collection_point_count(
    config: &QdrantConfig,
    collection: &str,
) -> anyhow::Result<Option<u64>> {
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
    Ok(parse_collection_point_count(&data))
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
    let Some(result) = data.get("result") else {
        return Err(QdrantError::OperationStatus {
            operation: "delete points",
            operation_status: "<missing result>".to_string(),
            collection: Some(collection.to_string()),
            request: Some(format!("POST {request_path}")),
        }
        .into());
    };
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
    Ok(())
}

/// Delete a whole Qdrant collection. Missing collections are already purged.
pub fn delete_collection(config: &QdrantConfig, collection: &str) -> anyhow::Result<()> {
    let request_path = collection_request_path(collection);
    let resp = qdrant_request(config, reqwest::Method::DELETE, &request_path)?.send()?;
    let status = resp.status();
    if status == StatusCode::NOT_FOUND {
        return Ok(());
    }
    if !status.is_success() {
        return Err(qdrant_http_error(
            "delete collection",
            status,
            resp,
            collection,
            request_path,
        ));
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

pub fn upsert_batched(
    config: &QdrantConfig,
    collection: &str,
    points: Vec<UpsertRequest>,
) -> anyhow::Result<usize> {
    upsert_batched_with_size(config, collection, points, DEFAULT_UPSERT_BATCH_SIZE)
}

pub fn upsert_batched_with_size(
    config: &QdrantConfig,
    collection: &str,
    points: Vec<UpsertRequest>,
    batch_size: usize,
) -> anyhow::Result<usize> {
    if points.is_empty() {
        return Ok(0);
    }

    let batch_size = batch_size.max(1);
    let mut upserted = 0;
    let mut remaining = points.into_iter();
    loop {
        let batch = remaining.by_ref().take(batch_size).collect::<Vec<_>>();
        if batch.is_empty() {
            break;
        }
        let requested = batch.len();
        upsert(config, collection, batch)?;
        upserted += requested;
    }

    Ok(upserted)
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

fn parse_collection_point_count(data: &Value) -> Option<u64> {
    data.pointer("/result/points_count")
        .and_then(Value::as_u64)
        .or_else(|| {
            data.pointer("/result/vectors_count")
                .and_then(Value::as_u64)
        })
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
        "delete collection" => "DELETE",
        "delete points" => "POST",
        "search" => "POST",
        "upsert" => "PUT",
        _ => "POST",
    }
}

#[cfg(test)]
mod tests;

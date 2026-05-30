use reqwest::StatusCode;
use serde_json::{Value, json};
use std::time::Duration;

use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, QdrantConfig};
use gobby_core::qdrant::{CollectionScope, SearchRequest};

use super::types::{ExistingVectorCollectionSchema, VectorLifecycleError};

// Keep code-symbol collections compatible with the Python daemon's Qdrant schema.
pub const VECTOR_DISTANCE_COSINE: &str = "Cosine";
const HTTP_TIMEOUT: Duration = Duration::from_secs(10);

pub fn collection_name(collection_prefix: &str, project_id: &str) -> String {
    let collection = format!("{collection_prefix}{project_id}");
    gobby_core::qdrant::collection_name("gcode", CollectionScope::Custom(&collection))
}

pub fn delete_project_collection(
    qdrant: &QdrantConfig,
    project_id: &str,
) -> Result<bool, VectorLifecycleError> {
    let client = qdrant_http_client()?;
    let collection = collection_name(CODE_SYMBOL_COLLECTION_PREFIX, project_id);
    delete_qdrant_collection(&client, qdrant, &collection)
}

pub fn delete_file_vectors(
    qdrant: &QdrantConfig,
    project_id: &str,
    file_path: &str,
) -> Result<bool, VectorLifecycleError> {
    let client = qdrant_http_client()?;
    let collection = collection_name(CODE_SYMBOL_COLLECTION_PREFIX, project_id);
    delete_vectors_for_filter(&client, qdrant, &collection, project_id, Some(file_path))
}

pub fn delete_code_symbol_collections_with_prefix(
    qdrant: &QdrantConfig,
) -> Result<Vec<String>, VectorLifecycleError> {
    let client = qdrant_http_client()?;
    let resp = qdrant_request_for_config(&client, qdrant, reqwest::Method::GET, "/collections")?
        .send()
        .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
    let status = resp.status();
    if !status.is_success() {
        return Err(qdrant_http_error("list collections", status, resp));
    }

    let data: Value = resp
        .json()
        .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
    let collections = parse_collection_names(&data)
        .into_iter()
        .filter(|name| name.starts_with(CODE_SYMBOL_COLLECTION_PREFIX))
        .collect::<Vec<_>>();

    let mut deleted = Vec::new();
    for collection in collections {
        if delete_qdrant_collection(&client, qdrant, &collection)? {
            deleted.push(collection);
        }
    }
    Ok(deleted)
}

pub fn vector_search(
    config: &QdrantConfig,
    collection: &str,
    query_vector: &[f32],
    limit: usize,
) -> anyhow::Result<Vec<(String, f64)>> {
    let request = SearchRequest {
        vector: query_vector.to_vec(),
        limit,
        filter: None,
    };
    let (hits, _) = gobby_core::qdrant::with_qdrant(Some(config), Vec::new(), |config| {
        gobby_core::qdrant::search(config, collection, request)
    })?;
    Ok(hits
        .into_iter()
        .map(|hit| (hit.id, f64::from(hit.score)))
        .collect())
}

pub(super) fn parse_collection_schema(data: &Value) -> Option<ExistingVectorCollectionSchema> {
    let vectors = data.pointer("/result/config/params/vectors")?;
    let size = vectors
        .get("size")
        .and_then(Value::as_u64)
        .and_then(|size| usize::try_from(size).ok());
    let distance = vectors
        .get("distance")
        .and_then(Value::as_str)
        .map(str::to_string);
    Some(ExistingVectorCollectionSchema { size, distance })
}

fn parse_collection_names(data: &Value) -> Vec<String> {
    data.pointer("/result/collections")
        .and_then(Value::as_array)
        .map(|collections| {
            collections
                .iter()
                .filter_map(|collection| {
                    collection
                        .get("name")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                })
                .collect()
        })
        .unwrap_or_default()
}

fn qdrant_http_client() -> Result<reqwest::blocking::Client, VectorLifecycleError> {
    reqwest::blocking::Client::builder()
        .timeout(HTTP_TIMEOUT)
        .build()
        .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))
}

pub(super) fn qdrant_request_for_config(
    client: &reqwest::blocking::Client,
    qdrant: &QdrantConfig,
    method: reqwest::Method,
    path: &str,
) -> Result<reqwest::blocking::RequestBuilder, VectorLifecycleError> {
    let base = qdrant
        .url
        .as_deref()
        .ok_or(VectorLifecycleError::MissingQdrantConfig)?
        .trim_end_matches('/');
    let url = format!("{base}{path}");
    let mut req = client.request(method, url);
    if let Some(key) = &qdrant.api_key {
        req = req.header("api-key", key);
    }
    Ok(req)
}

fn delete_qdrant_collection(
    client: &reqwest::blocking::Client,
    qdrant: &QdrantConfig,
    collection: &str,
) -> Result<bool, VectorLifecycleError> {
    let resp = qdrant_request_for_config(
        client,
        qdrant,
        reqwest::Method::DELETE,
        &format!("/collections/{collection}"),
    )?
    .send()
    .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
    let status = resp.status();
    if status == StatusCode::NOT_FOUND {
        return Ok(false);
    }
    if !status.is_success() {
        return Err(qdrant_http_error("delete collection", status, resp));
    }
    Ok(true)
}

pub(super) fn delete_vectors_for_filter(
    client: &reqwest::blocking::Client,
    qdrant: &QdrantConfig,
    collection: &str,
    project_id: &str,
    file_path: Option<&str>,
) -> Result<bool, VectorLifecycleError> {
    delete_vectors_for_filter_excluding_ids(client, qdrant, collection, project_id, file_path, &[])
}

pub(super) fn delete_vectors_for_filter_excluding_ids(
    client: &reqwest::blocking::Client,
    qdrant: &QdrantConfig,
    collection: &str,
    project_id: &str,
    file_path: Option<&str>,
    keep_point_ids: &[String],
) -> Result<bool, VectorLifecycleError> {
    let mut must = vec![json!({
        "key": "project_id",
        "match": {"value": project_id},
    })];
    if let Some(file_path) = file_path {
        must.push(json!({
            "key": "file_path",
            "match": {"value": file_path},
        }));
    }
    let mut filter = json!({ "must": must });
    if !keep_point_ids.is_empty()
        && let Some(filter) = filter.as_object_mut()
    {
        filter.insert(
            "must_not".to_string(),
            json!([{ "has_id": keep_point_ids }]),
        );
    }
    let body = json!({ "filter": filter });
    let resp = qdrant_request_for_config(
        client,
        qdrant,
        reqwest::Method::POST,
        &format!("/collections/{collection}/points/delete"),
    )?
    .json(&body)
    .send()
    .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
    let status = resp.status();
    if status == StatusCode::NOT_FOUND {
        return Ok(false);
    }
    if !status.is_success() {
        return Err(qdrant_http_error("delete points", status, resp));
    }
    Ok(true)
}

pub(super) fn qdrant_http_error(
    operation: &'static str,
    status: StatusCode,
    resp: reqwest::blocking::Response,
) -> VectorLifecycleError {
    VectorLifecycleError::QdrantHttp {
        operation,
        status: status.as_u16(),
        body: resp.text().unwrap_or_default(),
    }
}

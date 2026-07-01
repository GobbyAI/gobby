use reqwest::StatusCode;
use serde_json::{Value, json};
use std::collections::{BTreeSet, HashSet};
use std::sync::OnceLock;
use std::time::Duration;

use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, QdrantConfig};
use gobby_core::degradation::ServiceState;
use gobby_core::qdrant::{CollectionNameError, CollectionScope, SearchRequest};

use super::types::{ExistingVectorCollectionSchema, VectorLifecycleError};

// Keep code-symbol collections compatible with the Python daemon's Qdrant schema.
pub const VECTOR_DISTANCE_COSINE: &str = "Cosine";
const QDRANT_DELETE_TIMEOUT_SECS_ENV: &str = "GCODE_QDRANT_DELETE_TIMEOUT_SECS";
const DEFAULT_QDRANT_DELETE_TIMEOUT: Duration = Duration::from_secs(60);
const QDRANT_SCROLL_LIMIT: usize = 256;
static QDRANT_HTTP_CLIENT: OnceLock<reqwest::blocking::Client> = OnceLock::new();

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VectorOrphanCleanup {
    pub project_id: String,
    pub collection: String,
    pub vector_files_scanned: usize,
    pub orphan_files_deleted: usize,
    pub vectors_deleted: usize,
}

pub fn collection_name(
    collection_prefix: &str,
    project_id: &str,
) -> Result<String, CollectionNameError> {
    let collection = format!("{collection_prefix}{project_id}");
    gobby_core::qdrant::collection_name("gcode", CollectionScope::Custom(&collection))
}

pub(super) fn collection_path(collection: &str) -> String {
    format!("/collections/{}", urlencoding::encode(collection))
}

pub fn delete_project_collection(
    qdrant: &QdrantConfig,
    project_id: &str,
) -> Result<usize, VectorLifecycleError> {
    let client = qdrant_http_client()?;
    let collection = collection_name(CODE_SYMBOL_COLLECTION_PREFIX, project_id)?;
    delete_qdrant_collection(&client, qdrant, &collection)
}

pub fn delete_file_vectors(
    qdrant: &QdrantConfig,
    project_id: &str,
    file_path: &str,
) -> Result<usize, VectorLifecycleError> {
    let client = qdrant_http_client()?;
    let collection = collection_name(CODE_SYMBOL_COLLECTION_PREFIX, project_id)?;
    delete_vectors_for_filter(&client, qdrant, &collection, project_id, Some(file_path))
}

pub fn cleanup_orphan_file_vectors(
    qdrant: &QdrantConfig,
    project_id: &str,
    indexed_file_paths: &HashSet<String>,
) -> Result<VectorOrphanCleanup, VectorLifecycleError> {
    let vector_file_paths = list_project_vector_file_paths(qdrant, project_id)?;
    let collection = collection_name(CODE_SYMBOL_COLLECTION_PREFIX, project_id)?;
    let mut orphan_files_deleted = 0;
    let mut vectors_deleted = 0;

    for file_path in vector_file_paths
        .iter()
        .filter(|file_path| !indexed_file_paths.contains(*file_path))
    {
        orphan_files_deleted += 1;
        vectors_deleted += delete_file_vectors(qdrant, project_id, file_path)?;
    }

    Ok(VectorOrphanCleanup {
        project_id: project_id.to_string(),
        collection,
        vector_files_scanned: vector_file_paths.len(),
        orphan_files_deleted,
        vectors_deleted,
    })
}

pub(super) fn list_project_vector_file_paths(
    qdrant: &QdrantConfig,
    project_id: &str,
) -> Result<BTreeSet<String>, VectorLifecycleError> {
    let client = qdrant_http_client()?;
    let collection = collection_name(CODE_SYMBOL_COLLECTION_PREFIX, project_id)?;
    let mut file_paths = BTreeSet::new();
    let mut offset = None;

    loop {
        let mut body = json!({
            "filter": {
                "must": [
                    {
                        "key": "project_id",
                        "match": {"value": project_id},
                    },
                ],
            },
            "with_payload": ["project_id", "file_path"],
            "with_vector": false,
            "limit": QDRANT_SCROLL_LIMIT,
        });
        if let Some(next_offset) = offset.take() {
            body["offset"] = next_offset;
        }

        let resp = qdrant_request_for_config(
            &client,
            qdrant,
            reqwest::Method::POST,
            &format!("{}/points/scroll", collection_path(&collection)),
        )?
        .json(&body)
        .send()
        .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
        let status = resp.status();
        if status == StatusCode::NOT_FOUND {
            return Ok(file_paths);
        }
        if !status.is_success() {
            return Err(qdrant_http_error("scroll points", status, resp));
        }

        let data: Value = resp
            .json()
            .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
        collect_file_paths_from_scroll_page(&data, &mut file_paths)?;
        offset = data
            .pointer("/result/next_page_offset")
            .filter(|offset| !offset.is_null())
            .cloned();
        if offset.is_none() {
            return Ok(file_paths);
        }
    }
}

fn collect_file_paths_from_scroll_page(
    data: &Value,
    file_paths: &mut BTreeSet<String>,
) -> Result<(), VectorLifecycleError> {
    let points = data
        .pointer("/result/points")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            VectorLifecycleError::QdrantOperation(
                "missing result.points in Qdrant scroll response".to_string(),
            )
        })?;
    for point in points {
        if let Some(file_path) = point.pointer("/payload/file_path").and_then(Value::as_str) {
            file_paths.insert(file_path.to_string());
        }
    }
    Ok(())
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
        if delete_qdrant_collection(&client, qdrant, &collection)? > 0 {
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
    let (hits, state) = gobby_core::qdrant::with_qdrant(Some(config), Vec::new(), |config| {
        gobby_core::qdrant::search(config, collection, request)
    })?;
    if let Some(message) = vector_search_degradation_warning(&state) {
        log::warn!("{message}");
    }
    Ok(hits
        .into_iter()
        .map(|hit| (hit.id, f64::from(hit.score)))
        .collect())
}

fn vector_search_degradation_warning(state: &ServiceState) -> Option<String> {
    match state {
        ServiceState::Available => None,
        ServiceState::NotConfigured => {
            Some("semantic vector search skipped: Qdrant is not configured".to_string())
        }
        ServiceState::Unreachable { message } => Some(format!(
            "semantic vector search skipped: Qdrant is unreachable: {message}"
        )),
    }
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

fn parse_points_count(data: &Value) -> Result<usize, VectorLifecycleError> {
    data.pointer("/result/count")
        .and_then(Value::as_u64)
        .and_then(|count| usize::try_from(count).ok())
        .ok_or_else(|| {
            VectorLifecycleError::QdrantOperation(
                "count points response did not include result.count".to_string(),
            )
        })
}

fn qdrant_http_client() -> Result<reqwest::blocking::Client, VectorLifecycleError> {
    if let Some(client) = QDRANT_HTTP_CLIENT.get() {
        return Ok(client.clone());
    }
    let client = reqwest::blocking::Client::builder()
        .timeout(qdrant_delete_timeout())
        .build()
        .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
    let _ = QDRANT_HTTP_CLIENT.set(client.clone());
    Ok(client)
}

fn qdrant_delete_timeout() -> Duration {
    std::env::var(QDRANT_DELETE_TIMEOUT_SECS_ENV)
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|seconds| *seconds > 0)
        .map(Duration::from_secs)
        .unwrap_or(DEFAULT_QDRANT_DELETE_TIMEOUT)
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
        .map(str::trim)
        .filter(|url| !url.is_empty())
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
) -> Result<usize, VectorLifecycleError> {
    let resp = qdrant_request_for_config(
        client,
        qdrant,
        reqwest::Method::DELETE,
        &collection_path(collection),
    )?
    .send()
    .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
    let status = resp.status();
    if status == StatusCode::NOT_FOUND {
        return Ok(0);
    }
    if !status.is_success() {
        return Err(qdrant_http_error("delete collection", status, resp));
    }
    Ok(1)
}

pub(super) fn delete_vectors_for_filter(
    client: &reqwest::blocking::Client,
    qdrant: &QdrantConfig,
    collection: &str,
    project_id: &str,
    file_path: Option<&str>,
) -> Result<usize, VectorLifecycleError> {
    delete_vectors_for_filter_excluding_ids(client, qdrant, collection, project_id, file_path, &[])
}

pub(super) fn delete_vectors_for_filter_excluding_ids(
    client: &reqwest::blocking::Client,
    qdrant: &QdrantConfig,
    collection: &str,
    project_id: &str,
    file_path: Option<&str>,
    keep_point_ids: &[String],
) -> Result<usize, VectorLifecycleError> {
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
    let mut filter_object = serde_json::Map::new();
    filter_object.insert("must".to_string(), Value::Array(must));
    if !keep_point_ids.is_empty() {
        filter_object.insert(
            "must_not".to_string(),
            json!([{ "has_id": keep_point_ids }]),
        );
    }
    let filter = Value::Object(filter_object);
    let count_body = json!({ "filter": filter.clone(), "exact": true });
    let resp = qdrant_request_for_config(
        client,
        qdrant,
        reqwest::Method::POST,
        &format!("{}/points/count", collection_path(collection)),
    )?
    .json(&count_body)
    .send()
    .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
    let status = resp.status();
    if status == StatusCode::NOT_FOUND {
        return Ok(0);
    }
    if !status.is_success() {
        return Err(qdrant_http_error("count points", status, resp));
    }
    let data: Value = resp
        .json()
        .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
    let count = parse_points_count(&data)?;
    if count == 0 {
        return Ok(0);
    }

    let body = json!({ "filter": filter });
    let resp = qdrant_request_for_config(
        client,
        qdrant,
        reqwest::Method::POST,
        &format!("{}/points/delete?wait=true", collection_path(collection)),
    )?
    .json(&body)
    .send()
    .map_err(|err| VectorLifecycleError::QdrantOperation(err.to_string()))?;
    let status = resp.status();
    if status == StatusCode::NOT_FOUND {
        return Ok(0);
    }
    if !status.is_success() {
        return Err(qdrant_http_error("delete points", status, resp));
    }
    Ok(count)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vector_search_degradation_warning_mentions_missing_qdrant_config() {
        assert_eq!(
            vector_search_degradation_warning(&ServiceState::NotConfigured),
            Some("semantic vector search skipped: Qdrant is not configured".to_string())
        );
    }

    #[test]
    fn vector_search_degradation_warning_mentions_unreachable_qdrant() {
        assert_eq!(
            vector_search_degradation_warning(&ServiceState::Unreachable {
                message: "connection refused".to_string()
            }),
            Some(
                "semantic vector search skipped: Qdrant is unreachable: connection refused"
                    .to_string()
            )
        );
    }
}

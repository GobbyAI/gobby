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

#[derive(Debug, thiserror::Error)]
pub enum QdrantError {
    #[error("Qdrant {operation} failed: HTTP {status}")]
    HttpStatus {
        operation: &'static str,
        status: reqwest::StatusCode,
        body: String,
    },
}

/// Scope for a Qdrant collection, allowing caller-controlled naming.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CollectionScope<'a> {
    /// `{namespace}:project:{id}` — per-project vector store.
    Project(&'a str),
    /// `{namespace}:topic:{name}` — topic-scoped store.
    Topic(&'a str),
    /// Verbatim collection name, without namespace prefixing.
    Custom(&'a str),
}

/// Build a collection name from namespace and scope.
pub fn collection_name(namespace: &str, scope: CollectionScope<'_>) -> String {
    match scope {
        CollectionScope::Project(id) => format!("{namespace}:project:{id}"),
        CollectionScope::Topic(name) => format!("{namespace}:topic:{name}"),
        CollectionScope::Custom(name) => name.to_string(),
    }
}

/// Vector upsert request with opaque domain payload.
#[derive(Debug, Clone, PartialEq)]
pub struct UpsertRequest {
    pub id: String,
    pub vector: Vec<f32>,
    pub payload: Map<String, Value>,
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

    let mut req = client.post(format!(
        "{url}/collections/{}/points/search",
        encoded_collection(collection)
    ));
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

/// Execute a batch vector upsert via Qdrant REST API.
pub fn upsert(
    config: &QdrantConfig,
    collection: &str,
    points: Vec<UpsertRequest>,
) -> anyhow::Result<()> {
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

    let mut req = client.put(format!(
        "{url}/collections/{}/points",
        encoded_collection(collection)
    ));
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
        }
        .into());
    }

    Ok(())
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::QdrantConfig;
    use crate::degradation::ServiceState;
    use serde_json::{Map, Value, json};
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;

    #[test]
    fn collection_name_covers_all_scopes() {
        assert_eq!(
            collection_name("gwiki", CollectionScope::Project("abc-123")),
            "gwiki:project:abc-123"
        );
        assert_eq!(
            collection_name("gwiki", CollectionScope::Topic("rust-async")),
            "gwiki:topic:rust-async"
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
        let request = request_handle.join().expect("request thread");

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
        request_handle.join().expect("request thread");

        assert_eq!(state, ServiceState::Available);
        assert_eq!(hits[0].id, "point-1");
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
        request_handle.join().expect("request thread");

        assert!(hits.is_empty());
        assert!(matches!(
            state,
            ServiceState::Unreachable { ref message }
                if message.contains("Qdrant search failed: HTTP 503")
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
        search_handle.join().expect("search request thread");
        match err.downcast_ref::<QdrantError>() {
            Some(QdrantError::HttpStatus {
                operation,
                status,
                body,
            }) => {
                assert_eq!(*operation, "search");
                assert_eq!(*status, reqwest::StatusCode::SERVICE_UNAVAILABLE);
                assert!(body.contains("service unavailable"));
            }
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
        upsert_handle.join().expect("upsert request thread");
        match err.downcast_ref::<QdrantError>() {
            Some(QdrantError::HttpStatus {
                operation,
                status,
                body,
            }) => {
                assert_eq!(*operation, "upsert");
                assert_eq!(*status, reqwest::StatusCode::INTERNAL_SERVER_ERROR);
                assert!(body.contains("boom"));
            }
            None => panic!("expected QdrantError, got {err}"),
        }
    }

    #[test]
    fn qdrant_http_status_unreachable_only_for_server_errors() {
        let client_error = anyhow::Error::new(QdrantError::HttpStatus {
            operation: "search",
            status: reqwest::StatusCode::BAD_REQUEST,
            body: "bad request".to_string(),
        });
        let server_error = anyhow::Error::new(QdrantError::HttpStatus {
            operation: "search",
            status: reqwest::StatusCode::INTERNAL_SERVER_ERROR,
            body: "boom".to_string(),
        });

        assert!(!is_qdrant_unreachable(&client_error));
        assert!(is_qdrant_unreachable(&server_error));
    }

    fn spawn_qdrant_response(status: u16, body: Value) -> (String, thread::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
        let addr = listener.local_addr().expect("local addr");
        let handle = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept request");
            let request = read_http_request(&mut stream);

            let body = body.to_string();
            write!(
                stream,
                "HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .expect("write response");

            request
        });

        (format!("http://{addr}"), handle)
    }

    fn read_http_request(stream: &mut impl Read) -> String {
        let mut request = Vec::new();
        let mut buffer = [0; 4096];
        let mut expected_len = None;

        loop {
            let n = stream.read(&mut buffer).expect("read request");
            if n == 0 {
                break;
            }
            request.extend_from_slice(&buffer[..n]);

            if expected_len.is_none()
                && let Some(header_end) =
                    request.windows(4).position(|window| window == b"\r\n\r\n")
            {
                let headers = String::from_utf8_lossy(&request[..header_end]);
                let content_len = headers
                    .lines()
                    .find_map(|line| line.strip_prefix("content-length: "))
                    .and_then(|value| value.parse::<usize>().ok())
                    .unwrap_or(0);
                expected_len = Some(header_end + 4 + content_len);
            }

            if let Some(expected_len) = expected_len
                && request.len() >= expected_len
            {
                break;
            }
        }

        String::from_utf8_lossy(&request).into_owned()
    }
}

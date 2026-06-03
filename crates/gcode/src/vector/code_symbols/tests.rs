use super::*;
use crate::config::{
    CODE_SYMBOL_COLLECTION_PREFIX, CodeVectorSettings, Context, EmbeddingConfig, QdrantConfig,
};
use crate::models::{ProjectionProvenance, SOURCE_SYSTEM_GCODE, Symbol};
use serde_json::{Value, json};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::thread;
use std::time::{Duration, Instant};

fn test_symbol(summary: Option<String>) -> Symbol {
    Symbol {
        id: "symbol-1".to_string(),
        project_id: "project-1".to_string(),
        file_path: "src/lib.rs".to_string(),
        name: "run".to_string(),
        qualified_name: "crate::run".to_string(),
        kind: "function".to_string(),
        language: "rust".to_string(),
        byte_start: 10,
        byte_end: 40,
        line_start: 3,
        line_end: 5,
        signature: None,
        docstring: None,
        parent_symbol_id: None,
        content_hash: "hash".to_string(),
        summary,
        created_at: String::new(),
        updated_at: String::new(),
    }
}

#[test]
fn payloads_carry_provenance_metadata() {
    let payload = CodeSymbolVectorPayload::from_symbol(&test_symbol(Some("does work".into())));

    assert_eq!(payload.provenance, ProjectionProvenance::Extracted);
    assert_eq!(payload.confidence, Some(1.0));
    assert_eq!(payload.source_system, SOURCE_SYSTEM_GCODE);
    assert_eq!(payload.source_file_path, "src/lib.rs");
    assert_eq!(payload.source_line_start, 3);
    assert_eq!(payload.source_line_end, 5);
    assert_eq!(payload.source_byte_start, 10);
    assert_eq!(payload.source_byte_end, 40);
    assert_eq!(payload.source_line, 3);
    assert_eq!(payload.source_symbol_id, "symbol-1");
    assert_eq!(payload.summary.as_deref(), Some("does work"));
    assert_eq!(payload.signature, None);
    assert_eq!(payload.docstring, None);

    let value = serde_json::to_value(payload).expect("payload serializes");
    assert_eq!(value["provenance"], "EXTRACTED");
    assert_eq!(value["confidence"], 1.0);
    assert_eq!(value["source_system"], SOURCE_SYSTEM_GCODE);
    assert_eq!(value["source_file_path"], "src/lib.rs");
    assert_eq!(value["source_line_start"], 3);
    assert_eq!(value["source_line_end"], 5);
    assert_eq!(value["source_byte_start"], 10);
    assert_eq!(value["source_byte_end"], 40);
    assert_eq!(value["source_symbol_id"], "symbol-1");
}

#[test]
fn summaries_are_optional_enrichment() {
    let symbol = test_symbol(None);
    let payload = CodeSymbolVectorPayload::from_symbol(&symbol);
    let vector_text = vector_text_for_symbol(&symbol);
    let value = serde_json::to_value(payload).expect("payload serializes");

    assert!(value.get("summary").is_none());
    assert!(vector_text.contains("name: run"));
    assert!(!vector_text.contains("summary:"));
}

#[test]
fn collection_name_compatibility() {
    assert_eq!(
        collection_name(CODE_SYMBOL_COLLECTION_PREFIX, "project-1"),
        "code_symbols_project-1"
    );
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

fn test_context(qdrant: Option<QdrantConfig>) -> Context {
    Context {
        database_url: "postgresql://localhost/nonexistent".to_string(),
        project_root: PathBuf::from("/nonexistent"),
        project_id: "project-1".to_string(),
        quiet: true,
        falkordb: None,
        qdrant,
        embedding: None,
        code_vectors: CodeVectorSettings::default(),
        daemon_url: None,
        index_scope: crate::config::ProjectIndexScope::Single,
    }
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

#[test]
fn embedding_request_response() {
    let (base_url, handle) = spawn_http_responses(vec![(
        200,
        json!({"data": [{"embedding": [0.25, 0.5, 0.75]}]}),
    )]);
    let config = EmbeddingConfig {
        api_base: format!("{base_url}/v1"),
        model: "embed-small".to_string(),
        api_key: Some("embedding-key".to_string()),
        query_prefix: None,
        timeout_seconds: 10,
    };

    let client = embedding_client(&config).expect("embedding client");
    let embedding = embed_text(&client, &config, "dimension_probe").expect("embedding response");
    let requests = handle.join().expect("server thread");

    assert_eq!(embedding, vec![0.25, 0.5, 0.75]);
    assert_eq!(requests.len(), 1);
    assert!(requests[0].contains("POST /v1/embeddings HTTP/1.1"));
    assert!(requests[0].contains("authorization: Bearer embedding-key"));
    assert!(requests[0].contains(r#""model":"embed-small""#));
    assert!(requests[0].contains(r#""input":"dimension_probe""#));
}

#[test]
fn embedding_batch_preserves_response_index_order() {
    let (base_url, handle) = spawn_http_responses(vec![(
        200,
        json!({
            "data": [
                {"index": 1, "embedding": [0.2, 0.22]},
                {"index": 0, "embedding": [0.1, 0.11]}
            ]
        }),
    )]);
    let config = EmbeddingConfig {
        api_base: format!("{base_url}/v1"),
        model: "embed-small".to_string(),
        api_key: None,
        query_prefix: None,
        timeout_seconds: 10,
    };
    let client = embedding_client(&config).expect("embedding client");

    let embeddings = embed_text_batch(
        &client,
        &config,
        &["first".to_string(), "second".to_string()],
    )
    .expect("batch embedding response");
    let requests = handle.join().expect("server thread");

    assert_eq!(embeddings, vec![vec![0.1, 0.11], vec![0.2, 0.22]]);
    assert!(requests[0].contains(r#""input":["first","second"]"#));
}

#[test]
fn sync_rejects_embedding_vectors_with_wrong_dimension() {
    let (embedding_url, embedding_handle) =
        spawn_http_responses(vec![(200, json!({"data": [{"embedding": [0.1, 0.2]}]}))]);
    let (qdrant_url, qdrant_handle) = spawn_http_responses(vec![(
        200,
        json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}}),
    )]);
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

    let err = lifecycle
        .sync_file_symbols("src/lib.rs", &[test_symbol(None)])
        .expect_err("wrong vector dimension must fail before upsert");
    let embedding_requests = embedding_handle.join().expect("embedding requests");
    let qdrant_requests = qdrant_handle.join().expect("qdrant requests");

    assert!(
        matches!(err, VectorLifecycleError::EmbeddingResponse(message) if message.contains("expected 3"))
    );
    assert_eq!(embedding_requests.len(), 1);
    assert_eq!(qdrant_requests.len(), 1);
    assert!(!qdrant_requests[0].contains("/points"));
}

#[test]
fn sync_rejects_embedding_vector_count_mismatch() {
    let (embedding_url, embedding_handle) = spawn_http_responses(vec![(
        200,
        json!({"data": [{"embedding": [0.1, 0.2, 0.3]}]}),
    )]);
    let (qdrant_url, qdrant_handle) = spawn_http_responses(vec![(
        200,
        json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}}),
    )]);
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
    let mut second = test_symbol(None);
    second.id = "symbol-2".to_string();

    let err = lifecycle
        .sync_file_symbols("src/lib.rs", &[test_symbol(None), second])
        .expect_err("vector count mismatch must fail before upsert");
    let embedding_requests = embedding_handle.join().expect("embedding requests");
    let qdrant_requests = qdrant_handle.join().expect("qdrant requests");

    let VectorLifecycleError::EmbeddingResponse(message) = &err else {
        panic!("unexpected error: {err}");
    };
    assert!(
        message.contains("1 vector(s) for 2 symbol(s)")
            || message.contains("1 vector(s) for 2 input(s)")
    );
    assert_eq!(embedding_requests.len(), 1);
    assert_eq!(qdrant_requests.len(), 1);
    assert!(!qdrant_requests[0].contains("/points"));
}

#[test]
fn dim_probe_with_override() {
    let (embedding_url, embedding_handle) = spawn_http_responses(vec![(
        200,
        json!({"data": [{"embedding": [0.1, 0.2, 0.3]}]}),
    )]);
    let (qdrant_url, qdrant_handle) = spawn_http_responses(vec![
        (404, json!({"status": "not found"})),
        (200, json!({"result": true})),
        (
            200,
            json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}}),
        ),
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
        CodeVectorSettings { vector_dim: None },
    )
    .expect("lifecycle");

    let created = lifecycle.ensure_collection().expect("create collection");
    let reused = lifecycle.ensure_collection().expect("reuse collection");
    let embedding_requests = embedding_handle.join().expect("embedding requests");
    let qdrant_requests = qdrant_handle.join().expect("qdrant requests");

    assert_eq!(created.size, 3);
    assert_eq!(created.distance, VECTOR_DISTANCE_COSINE);
    assert_eq!(reused.size, 3);
    assert_eq!(embedding_requests.len(), 1, "dimension probe is cached");
    assert!(qdrant_requests[1].contains("PUT /collections/code_symbols_project-1 HTTP/1.1"));
    assert!(qdrant_requests[1].contains(r#""size":3"#));
    assert!(qdrant_requests[1].contains(r#""distance":"Cosine""#));

    let (explicit_qdrant_url, explicit_handle) = spawn_http_responses(vec![
        (404, json!({"status": "not found"})),
        (200, json!({"result": true})),
    ]);
    let mut explicit = CodeSymbolVectorLifecycle::new(
        "project-1".to_string(),
        QdrantConfig {
            url: Some(explicit_qdrant_url),
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
            vector_dim: Some(1536),
        },
    )
    .expect("lifecycle with explicit size");

    let schema = explicit.ensure_collection().expect("explicit size create");
    let explicit_requests = explicit_handle.join().expect("explicit qdrant requests");
    assert_eq!(schema.size, 1536);
    assert!(explicit_requests[1].contains(r#""size":1536"#));
}

#[test]
fn lifecycle_http_scoped_to_module() {
    let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let src_dir = manifest_dir.join("src");
    let mut offenders = Vec::new();

    fn visit(
        path: &std::path::Path,
        src_dir: &std::path::Path,
        offenders: &mut Vec<std::path::PathBuf>,
    ) {
        for entry in std::fs::read_dir(path).expect("read source directory") {
            let entry = entry.expect("source entry");
            let path = entry.path();
            if path.is_dir() {
                visit(&path, src_dir, offenders);
                continue;
            }
            if path.extension().and_then(|ext| ext.to_str()) != Some("rs") {
                continue;
            }
            let source = std::fs::read_to_string(&path).expect("read source file");
            let searchable_source = rust_code_without_comments_and_literals(&source);
            let lifecycle_rest = [
                "/points/delete",
                "points/delete",
                "collections/{collection}",
                "/collections/{collection}",
            ];
            let rel_path = path.strip_prefix(src_dir).unwrap_or(&path);
            let allowed_module_dir = std::path::Path::new("vector").join("code_symbols");
            let allowed_facade = std::path::Path::new("vector").join("code_symbols.rs");
            if lifecycle_rest
                .iter()
                .any(|needle| searchable_source.contains(needle))
                && rel_path != allowed_facade
                && !rel_path.starts_with(&allowed_module_dir)
            {
                offenders.push(path);
            }
        }
    }

    visit(&src_dir, &src_dir, &mut offenders);
    assert!(
        offenders.is_empty(),
        "Qdrant lifecycle REST must stay scoped to vector/code_symbols module: {offenders:?}"
    );
}

fn rust_code_without_comments_and_literals(source: &str) -> String {
    let bytes = source.as_bytes();
    let mut output = String::with_capacity(source.len());
    let mut index = 0;

    while index < bytes.len() {
        if bytes.get(index..index + 2) == Some(b"//") {
            let start = index;
            index += 2;
            while index < bytes.len() && bytes[index] != b'\n' {
                index += 1;
            }
            push_masked(&mut output, &bytes[start..index]);
            continue;
        }
        if bytes.get(index..index + 2) == Some(b"/*") {
            let start = index;
            index = skip_block_comment(bytes, index + 2);
            push_masked(&mut output, &bytes[start..index]);
            continue;
        }
        if let Some((prefix_len, hashes)) = raw_string_prefix(bytes, index) {
            let start = index;
            index += prefix_len;
            while index < bytes.len() {
                if bytes[index] == b'"' && raw_hashes_match(bytes, index + 1, hashes) {
                    index += 1 + hashes;
                    break;
                }
                index += 1;
            }
            push_masked(&mut output, &bytes[start..index]);
            continue;
        }
        if bytes.get(index..index + 2) == Some(b"b\"") {
            let start = index;
            index = skip_quoted_string(bytes, index + 2);
            push_masked(&mut output, &bytes[start..index]);
            continue;
        }
        if bytes[index] == b'"' {
            let start = index;
            index = skip_quoted_string(bytes, index + 1);
            push_masked(&mut output, &bytes[start..index]);
            continue;
        }
        if let Some(end) = char_literal_end(bytes, index) {
            push_masked(&mut output, &bytes[index..end]);
            index = end;
            continue;
        }

        output.push(bytes[index] as char);
        index += 1;
    }

    output
}

fn skip_block_comment(bytes: &[u8], mut index: usize) -> usize {
    let mut depth = 1usize;
    while index + 1 < bytes.len() {
        match &bytes[index..index + 2] {
            b"/*" => {
                depth += 1;
                index += 2;
            }
            b"*/" => {
                depth -= 1;
                index += 2;
                if depth == 0 {
                    return index;
                }
            }
            _ => index += 1,
        }
    }
    bytes.len()
}

fn raw_string_prefix(bytes: &[u8], index: usize) -> Option<(usize, usize)> {
    let raw_start = match bytes.get(index) {
        Some(b'r') => index,
        Some(b'b') if bytes.get(index + 1) == Some(&b'r') => index + 1,
        _ => return None,
    };
    let mut cursor = raw_start + 1;
    while bytes.get(cursor) == Some(&b'#') {
        cursor += 1;
    }
    (bytes.get(cursor) == Some(&b'"')).then_some((cursor - index + 1, cursor - raw_start - 1))
}

fn raw_hashes_match(bytes: &[u8], start: usize, hashes: usize) -> bool {
    start + hashes <= bytes.len()
        && bytes[start..start + hashes]
            .iter()
            .all(|byte| *byte == b'#')
}

fn skip_quoted_string(bytes: &[u8], mut index: usize) -> usize {
    let mut escaped = false;
    while index < bytes.len() {
        let byte = bytes[index];
        index += 1;
        if escaped {
            escaped = false;
        } else if byte == b'\\' {
            escaped = true;
        } else if byte == b'"' {
            break;
        }
    }
    index
}

fn char_literal_end(bytes: &[u8], index: usize) -> Option<usize> {
    let start = if bytes.get(index) == Some(&b'\'') {
        index
    } else if bytes.get(index..index + 2) == Some(b"b'") {
        index + 1
    } else {
        return None;
    };
    let cursor = match bytes.get(start + 1) {
        Some(b'\\') => escaped_char_literal_payload_end(bytes, start + 2)?,
        Some(_) => {
            let rest = std::str::from_utf8(&bytes[start + 1..]).ok()?;
            start + 1 + rest.chars().next()?.len_utf8()
        }
        None => return None,
    };
    (bytes.get(cursor) == Some(&b'\'')).then_some(cursor + 1)
}

fn escaped_char_literal_payload_end(bytes: &[u8], index: usize) -> Option<usize> {
    match bytes.get(index)? {
        b'x' => (index + 3 <= bytes.len()).then_some(index + 3),
        b'u' if bytes.get(index + 1) == Some(&b'{') => {
            let close = bytes[index + 2..].iter().position(|byte| *byte == b'}')?;
            Some(index + 2 + close + 1)
        }
        _ => Some(index + 1),
    }
}

fn push_masked(output: &mut String, bytes: &[u8]) {
    for byte in bytes {
        if *byte == b'\n' {
            output.push('\n');
        } else {
            output.push(' ');
        }
    }
}

#[test]
fn rust_literal_mask_handles_long_char_escapes() {
    let source = r#"let emoji = '\u{1F600}';
let byte = b'\x7f';
call_real_code();
"#;

    let masked = rust_code_without_comments_and_literals(source);

    assert!(!masked.contains(r"\u{1F600}"));
    assert!(!masked.contains(r"\x7f"));
    assert!(masked.contains("call_real_code();"));
}

#[test]
fn routes_through_gobby_core_qdrant() {
    let source = [include_str!("qdrant.rs"), include_str!("lifecycle.rs")].join("\n");
    assert!(source.contains("gobby_core::config::resolve_qdrant_config"));
    assert!(source.contains("gobby_core::qdrant::with_qdrant"));
    assert!(source.contains("gobby_core::qdrant::collection_name"));
    assert!(source.contains("CollectionScope::Custom"));
    assert!(source.contains("gobby_core::qdrant::search"));
    assert!(source.contains("gobby_core::qdrant::upsert"));
}

fn spawn_http_responses(responses: Vec<(u16, Value)>) -> (String, thread::JoinHandle<Vec<String>>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind test server");
    listener
        .set_nonblocking(true)
        .expect("set test server nonblocking");
    let addr = listener.local_addr().expect("local addr");
    let handle = thread::spawn(move || {
        let mut requests = Vec::new();
        for (status, body) in responses {
            let mut stream =
                accept_with_timeout(&listener, Duration::from_secs(5)).expect("accept request");
            requests.push(read_http_request(&mut stream));

            let reason = reqwest::StatusCode::from_u16(status)
                .ok()
                .and_then(|status| status.canonical_reason())
                .unwrap_or("OK");
            let body = body.to_string();
            write!(
                    stream,
                    "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                )
                .expect("write response");
        }
        requests
    });

    (format!("http://{addr}"), handle)
}

fn accept_with_timeout(listener: &TcpListener, timeout: Duration) -> std::io::Result<TcpStream> {
    let deadline = Instant::now() + timeout;
    loop {
        match listener.accept() {
            Ok((stream, _)) => return Ok(stream),
            Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => {
                if Instant::now() >= deadline {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::TimedOut,
                        "timed out waiting for test HTTP request",
                    ));
                }
                thread::sleep(Duration::from_millis(10));
            }
            Err(err) => return Err(err),
        }
    }
}

fn read_http_request(stream: &mut TcpStream) -> String {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("set read timeout");
    // Stop at Content-Length when present; otherwise the read timeout ends
    // keep-alive or bodyless test requests without hanging the server thread.
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
            && let Some(header_end) = request.windows(4).position(|window| window == b"\r\n\r\n")
        {
            let headers = String::from_utf8_lossy(&request[..header_end]);
            let content_len = headers
                .lines()
                .find_map(|line| {
                    line.to_ascii_lowercase()
                        .strip_prefix("content-length: ")
                        .and_then(|value| value.parse::<usize>().ok())
                })
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

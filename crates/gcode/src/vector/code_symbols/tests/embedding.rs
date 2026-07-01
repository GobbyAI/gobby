use super::*;
use serde_json::json;

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

use super::super::response::parse_daemon_embeddings;
use super::super::transport::LOCAL_TOKEN_HEADER;
use super::*;

#[test]
fn embeddings_post_preserves_batch_and_local_token() {
    let (port, request) =
        spawn_server(r#"{"embeddings":[[0.1,0.2],[0.3,0.4]],"model":"embed-model","dim":2}"#);
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "embed-token");
    let cfg = test_context(Some("project-123"));
    let input = vec!["same".to_string(), "same".to_string()];

    let result = embed_via_daemon(&cfg, &input, true).unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert!(request.starts_with("POST /api/embeddings HTTP/1.1"));
    assert!(has_header(&request, LOCAL_TOKEN_HEADER, "embed-token"));
    assert_eq!(body["input"], serde_json::json!(["same", "same"]));
    assert_eq!(body["is_query"], true);
    assert_eq!(body["project_id"], "project-123");
    assert_eq!(body["provider"], "daemon-provider");
    assert_eq!(body["model"], "daemon-model");
    assert_eq!(result.model, "embed-model");
    assert_eq!(result.dim, 2);
    assert_eq!(result.embeddings, vec![vec![0.1, 0.2], vec![0.3, 0.4]]);
}

#[test]
fn embedding_response_validates_count_and_dimension() {
    let wrong_count = parse_daemon_embeddings(
        serde_json::json!({
            "embeddings": [[0.1, 0.2]],
            "model": "embed-model",
            "dim": 2
        }),
        2,
    )
    .expect_err("count mismatch rejected");
    assert!(
        wrong_count
            .to_string()
            .contains("returned 1 vector(s) for 2 input(s)")
    );

    let wrong_dim = parse_daemon_embeddings(
        serde_json::json!({
            "embeddings": [[0.1]],
            "model": "embed-model",
            "dim": 2
        }),
        1,
    )
    .expect_err("dimension mismatch rejected");
    assert!(
        wrong_dim
            .to_string()
            .contains("returned 1 dimension(s), expected 2")
    );
}

#[test]
fn empty_embedding_batch_parses_daemon_model_and_dim() {
    let (port, request) = spawn_server(r#"{"embeddings":[],"model":"embed-model","dim":768}"#);
    let home = temp_home();
    let _env = EnvGuard::set_home(home.path());
    write_daemon_files(home.path(), port, "embed-token");
    let cfg = test_context(Some("project-123"));

    let result = embed_via_daemon(&cfg, &[], false).unwrap();
    let request = request.join().unwrap().unwrap();
    let body = request_body_json(&request);

    assert_eq!(body["input"], serde_json::json!([]));
    assert_eq!(result.model, "embed-model");
    assert_eq!(result.dim, 768);
    assert!(result.embeddings.is_empty());
}

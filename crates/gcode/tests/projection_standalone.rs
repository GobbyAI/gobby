mod common;

use common::http::spawn_http_responses;
use common::{ProjectCleanup, cleanup_project};
use gobby_core::config::embedding_keys;
use postgres::{Client, NoTls};
use serde_json::{Value, json};
use std::fs;
use std::process::{Command, Output};

// Fixture ids are UUIDv5(CODE_INDEX_UUID_NAMESPACE, <legacy label>) because the
// hub stores every code_* id column as native uuid.
const TEST_PROJECT_ID: &str = "ed4b112e-ef27-5198-a8e8-bbcf9a21c367"; // projection-standalone-project
const TEST_FILE_ID: &str = "594f29ce-5db4-533a-8f4d-0b4bb23b8452"; // projection-standalone-file
const CALLER_ID: &str = "7dc88f58-8509-5bce-ace8-3f8fa094a85f"; // projection-standalone-caller
const CALLEE_ID: &str = "5d199fbd-befb-5998-9b10-af5d05dcbad4"; // projection-standalone-callee
const TEST_FILE: &str = "src/lib.rs";

#[test]
fn graph_and_vector_lifecycle_commands_run_without_daemon() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping projection_standalone smoke; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let (embedding_url, embedding_handle) = spawn_http_responses(vec![
        (200, json!({"data": [{"embedding": [0.1, 0.2, 0.3]}]})),
        (200, json!({"data": [{"embedding": [0.4, 0.5, 0.6]}]})),
        (200, json!({"data": [{"embedding": [0.7, 0.8, 0.9]}]})),
        (200, json!({"data": [{"embedding": [0.2, 0.3, 0.4]}]})),
    ]);
    let (qdrant_url, qdrant_handle) = spawn_http_responses(vec![
        (404, json!({"status": "not found"})),
        (200, json!({"result": true})),
        (200, json!({"result": {"operation_id": 1}})),
        (200, json!({"result": {"operation_id": 2}})),
        (
            200,
            json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}}),
        ),
        (200, json!({"result": {"operation_id": 3}})),
        (
            200,
            json!({"result": {"config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}}}}),
        ),
        (200, json!({"result": {"operation_id": 4}})),
        (200, json!({"result": {"operation_id": 5}})),
        (
            200,
            json!({
                "result": {
                    "points": [
                        {"id": CALLER_ID, "payload": {"project_id": TEST_PROJECT_ID, "file_path": TEST_FILE}},
                        {"id": "stale-vector", "payload": {"project_id": TEST_PROJECT_ID, "file_path": "src/stale.rs"}}
                    ],
                    "next_page_offset": null
                }
            }),
        ),
        (200, json!({"result": {"count": 1}})),
        (200, json!({"result": {"operation_id": 6}})),
    ]);

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("src")).expect("create src");
    fs::write(
        project.path().join("src/lib.rs"),
        "pub fn caller() { callee(); }\npub fn callee() {}\n",
    )
    .expect("write source");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": TEST_PROJECT_ID,
            "name": "projection-standalone",
            "created_at": "2026-05-28T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");
    let gobby_home = project.path().join(".no-daemon-home");
    fs::create_dir_all(&gobby_home).expect("create gobby home");
    fs::write(
        gobby_home.join("grant-backed config"),
        format!(
            "{api_base}: {embedding_url}/v1\n{api_key}: test-key\n{model}: embed-small\n{dim}: 3\n",
            api_base = embedding_keys::AI_API_BASE,
            api_key = embedding_keys::AI_API_KEY,
            model = embedding_keys::AI_MODEL,
            dim = embedding_keys::AI_DIM,
        ),
    )
    .expect("write standalone config");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    if config_store_has_embedding_overrides(&mut conn) {
        eprintln!(
            "skipping projection_standalone smoke; config_store AI embedding keys override the mock grant-backed config"
        );
        return;
    }
    let _cleanup = ProjectCleanup::new(&env.database_url, TEST_PROJECT_ID);
    seed_project(&mut conn);

    let graph_sync = json_command(
        &env,
        project.path(),
        &qdrant_url,
        &embedding_url,
        &["graph", "sync-file", "--file", TEST_FILE],
    );
    assert_eq!(graph_sync["status"], "ok");
    assert_eq!(graph_sync["synced_files"], 1);
    assert_eq!(graph_sync["synced_symbols"], 2);

    let vector_sync = json_command(
        &env,
        project.path(),
        &qdrant_url,
        &embedding_url,
        &["vector", "sync-file", "--file", TEST_FILE],
    );
    assert_eq!(vector_sync["status"], "ok");
    assert_eq!(vector_sync["synced_files"], 1);
    assert_eq!(vector_sync["synced_symbols"], 2);

    let graph_clear = json_command(
        &env,
        project.path(),
        &qdrant_url,
        &embedding_url,
        &["graph", "clear"],
    );
    assert_eq!(graph_clear["status"], "ok");

    let graph_rebuild = json_command(
        &env,
        project.path(),
        &qdrant_url,
        &embedding_url,
        &["graph", "rebuild"],
    );
    assert_eq!(graph_rebuild["status"], "ok");
    assert_eq!(graph_rebuild["synced_files"], 1);
    assert_eq!(graph_rebuild["synced_symbols"], 2);

    let vector_clear = json_command(
        &env,
        project.path(),
        &qdrant_url,
        &embedding_url,
        &["vector", "clear"],
    );
    assert_eq!(vector_clear["status"], "ok");

    let vector_rebuild = json_command(
        &env,
        project.path(),
        &qdrant_url,
        &embedding_url,
        &["vector", "rebuild"],
    );
    assert_eq!(vector_rebuild["status"], "ok");
    assert_eq!(vector_rebuild["synced_files"], 1);
    assert_eq!(vector_rebuild["synced_symbols"], 2);

    let vector_cleanup = json_command(
        &env,
        project.path(),
        &qdrant_url,
        &embedding_url,
        &["vector", "cleanup-orphans"],
    );
    assert_eq!(vector_cleanup["status"], "ok");
    assert_eq!(vector_cleanup["vector_files_scanned"], 2);
    assert_eq!(vector_cleanup["orphan_files_deleted"], 1);
    assert_eq!(vector_cleanup["vectors_deleted"], 1);

    let embedding_requests = embedding_handle
        .join()
        .expect("embedding requests")
        .expect("embedding server");
    let qdrant_requests = qdrant_handle
        .join()
        .expect("qdrant requests")
        .expect("qdrant server");
    assert_eq!(embedding_requests.len(), 4);
    assert!(qdrant_requests.iter().any(|request| {
        request.contains(&format!(
            "PUT /collections/code_symbols_{TEST_PROJECT_ID} HTTP/1.1"
        ))
    }));
    assert!(
        qdrant_requests
            .iter()
            .any(|request| request.contains(&format!(
                "PUT /collections/code_symbols_{TEST_PROJECT_ID}/points HTTP/1.1"
            )))
    );
    assert!(
        qdrant_requests
            .iter()
            .any(|request| request.contains(&format!(
                "POST /collections/code_symbols_{TEST_PROJECT_ID}/points/scroll HTTP/1.1"
            )))
    );
    assert!(
        qdrant_requests
            .iter()
            .any(|request| request.contains(r#""value":"src/stale.rs""#))
    );
}

struct StandaloneEnv {
    database_url: String,
    falkor_host: String,
    falkor_port: String,
    falkor_password: Option<String>,
}

impl StandaloneEnv {
    fn from_env() -> Option<Self> {
        Some(Self {
            database_url: std::env::var("GCODE_GRAPH_STANDALONE_DATABASE_URL").ok()?,
            falkor_host: std::env::var("GCODE_GRAPH_STANDALONE_FALKOR_HOST").ok()?,
            falkor_port: std::env::var("GCODE_GRAPH_STANDALONE_FALKOR_PORT").ok()?,
            falkor_password: std::env::var("GCODE_GRAPH_STANDALONE_FALKOR_PASSWORD").ok(),
        })
    }
}

fn run_gcode(
    env: &StandaloneEnv,
    cwd: &std::path::Path,
    qdrant_url: &str,
    _embedding_url: &str,
    args: &[&str],
) -> Output {
    let home = cwd.join(".no-daemon-home");
    fs::create_dir_all(&home).expect("create no-daemon home");
    if !home.join("machine_id").exists() {
        fs::write(
            home.join("machine_id"),
            "ff5dd0ce-20a1-5f6c-8a89-ea85c2bbeea9",
        )
        .expect("write machine id");
    }
    let machine = fs::read_to_string(home.join("machine_id")).expect("read machine id");
    let port = env.falkor_port.parse::<i64>().unwrap_or(6379);
    let connections = gobby_core::grant::DirectConnections::postgres(&env.database_url)
        .with_falkor(&env.falkor_host, port, env.falkor_password.as_deref())
        .with_qdrant(qdrant_url, None);
    let grant =
        gobby_core::grant::managed_direct_grant(TEST_PROJECT_ID, machine.trim(), &connections);
    let grant_path = gobby_core::grant::write_managed_bootstrap(&home.join("grants"), &grant)
        .expect("write managed grant");
    let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
    command
        .current_dir(cwd)
        .env("GOBBY_HOME", home)
        .env("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", grant_path)
        .arg("--allow-stale")
        .arg("--format")
        .arg("json")
        .args(args);
    command.output().expect("run gcode")
}

fn json_command(
    env: &StandaloneEnv,
    cwd: &std::path::Path,
    qdrant_url: &str,
    embedding_url: &str,
    args: &[&str],
) -> Value {
    let output = run_gcode(env, cwd, qdrant_url, embedding_url, args);
    assert_success(output, &args.join(" "))
}

fn assert_success(output: Output, label: &str) -> Value {
    assert!(
        output.status.success(),
        "{label} failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap_or_else(|err| {
        panic!(
            "{label} did not emit JSON: {err}\nstdout:\n{}",
            String::from_utf8_lossy(&output.stdout)
        )
    })
}

fn config_store_has_embedding_overrides(conn: &mut Client) -> bool {
    conn.query_opt(
        "SELECT 1 FROM config_store
         WHERE key = $1 OR key = $2 OR key = $3 OR key = $4
         LIMIT 1",
        &[
            &embedding_keys::AI_API_BASE,
            &embedding_keys::AI_API_KEY,
            &embedding_keys::AI_MODEL,
            &embedding_keys::AI_DIM,
        ],
    )
    .map(|row| row.is_some())
    .unwrap_or(false)
}

fn seed_project(conn: &mut Client) {
    cleanup_project(conn, TEST_PROJECT_ID).expect("cleanup projection rows");
    conn.batch_execute(&format!(
        "INSERT INTO code_indexed_projects
            (id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms)
         VALUES
            ('{TEST_PROJECT_ID}', '/tmp/projection-standalone', 1, 2, NOW(), 0);

         INSERT INTO code_indexed_files
            (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
             graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
         VALUES
            ('{TEST_FILE_ID}', '{TEST_PROJECT_ID}', 'src/lib.rs', 'rust',
             'hash-1', 2, 54, false, false, NULL, NOW());

         INSERT INTO code_symbols
            (id, project_id, file_path, name, qualified_name, kind, language, byte_start, byte_end,
             line_start, line_end, signature, docstring, parent_symbol_id, content_hash,
             summary, created_at, updated_at)
         VALUES
            ('{CALLER_ID}', '{TEST_PROJECT_ID}', 'src/lib.rs', 'caller',
             'crate::caller', 'function', 'rust', 0, 28, 1, 1, 'pub fn caller()', NULL, NULL,
             'hash-1', NULL, NOW(), NOW()),
            ('{CALLEE_ID}', '{TEST_PROJECT_ID}', 'src/lib.rs', 'callee',
             'crate::callee', 'function', 'rust', 29, 47, 2, 2, 'pub fn callee()', NULL, NULL,
             'hash-1', NULL, NOW(), NOW());

         INSERT INTO code_imports (project_id, source_file, target_module)
         VALUES ('{TEST_PROJECT_ID}', 'src/lib.rs', 'std');

         INSERT INTO code_calls
            (project_id, caller_symbol_id, callee_symbol_id, callee_name, callee_target_kind,
             callee_external_module, file_path, line)
         VALUES
            ('{TEST_PROJECT_ID}', '{CALLER_ID}', '{CALLEE_ID}',
             'callee', 'symbol', '', 'src/lib.rs', 1);"
    ))
    .expect("seed projection rows");
}

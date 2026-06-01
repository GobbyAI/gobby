mod common;

use common::{ProjectCleanup, cleanup_project};
use gobby_core::config::embedding_keys;
use postgres::{Client, NoTls};
use serde_json::{Value, json};
use std::fs;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::{Command, Output};
use std::thread;
use std::time::{Duration, Instant};

const TEST_PROJECT_ID: &str = "projection-standalone-project";
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
        gobby_home.join("gcore.yaml"),
        format!(
            "{api_base}: {embedding_url}/v1\n{model}: embed-small\n{dim}: 3\n",
            api_base = embedding_keys::AI_API_BASE,
            model = embedding_keys::AI_MODEL,
            dim = embedding_keys::AI_DIM,
        ),
    )
    .expect("write standalone config");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
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

    let embedding_requests = embedding_handle.join().expect("embedding requests");
    let qdrant_requests = qdrant_handle.join().expect("qdrant requests");
    assert_eq!(embedding_requests.len(), 4);
    assert!(qdrant_requests.iter().any(|request| {
        request.contains("PUT /collections/code_symbols_projection-standalone-project HTTP/1.1")
    }));
    assert!(
        qdrant_requests.iter().any(|request| request.contains(
            "PUT /collections/code_symbols_projection-standalone-project/points HTTP/1.1"
        ))
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
    let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
    command
        .current_dir(cwd)
        .env("GCODE_DATABASE_URL", &env.database_url)
        .env("GOBBY_FALKORDB_HOST", &env.falkor_host)
        .env("GOBBY_FALKORDB_PORT", &env.falkor_port)
        .env("GOBBY_QDRANT_URL", qdrant_url)
        .env("GOBBY_HOME", cwd.join(".no-daemon-home"))
        .arg("--no-freshness")
        .arg("--format")
        .arg("json")
        .args(args);
    if let Some(password) = &env.falkor_password {
        command.env("GOBBY_FALKORDB_PASSWORD", password);
    }
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

fn seed_project(conn: &mut Client) {
    cleanup_project(conn, TEST_PROJECT_ID).expect("cleanup projection rows");
    conn.batch_execute(
        "INSERT INTO code_indexed_projects
            (id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms)
         VALUES
            ('projection-standalone-project', '/tmp/projection-standalone', 1, 2, NOW(), 0);

         INSERT INTO code_indexed_files
            (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
             graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
         VALUES
            ('projection-standalone-file', 'projection-standalone-project', 'src/lib.rs', 'rust',
             'hash-1', 2, 54, false, false, NULL, NOW());

         INSERT INTO code_symbols
            (id, project_id, file_path, name, qualified_name, kind, language, byte_start, byte_end,
             line_start, line_end, signature, docstring, parent_symbol_id, content_hash,
             summary, created_at, updated_at)
         VALUES
            ('projection-standalone-caller', 'projection-standalone-project', 'src/lib.rs', 'caller',
             'crate::caller', 'function', 'rust', 0, 28, 1, 1, 'pub fn caller()', NULL, NULL,
             'hash-1', NULL, NOW(), NOW()),
            ('projection-standalone-callee', 'projection-standalone-project', 'src/lib.rs', 'callee',
             'crate::callee', 'function', 'rust', 29, 47, 2, 2, 'pub fn callee()', NULL, NULL,
             'hash-1', NULL, NOW(), NOW());

         INSERT INTO code_imports (project_id, source_file, target_module)
         VALUES ('projection-standalone-project', 'src/lib.rs', 'std');

         INSERT INTO code_calls
            (project_id, caller_symbol_id, callee_symbol_id, callee_name, callee_target_kind,
             callee_external_module, file_path, line)
         VALUES
            ('projection-standalone-project', 'projection-standalone-caller', 'projection-standalone-callee',
             'callee', 'symbol', '', 'src/lib.rs', 1);",
    )
    .expect("seed projection rows");
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

            let body = body.to_string();
            write!(
                stream,
                "HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
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
        let n = match stream.read(&mut buffer) {
            Ok(n) => n,
            Err(err)
                if matches!(
                    err.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                break;
            }
            Err(err) => panic!("read request: {err}"),
        };
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

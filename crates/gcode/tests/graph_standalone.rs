use postgres::{Client, NoTls};
use serde_json::Value;
use std::fs;
use std::process::{Command, Output};

const TEST_PROJECT_ID: &str = "graph-standalone-project";
const TEST_FILE: &str = "src/lib.rs";
const CALLER_ID: &str = "graph-standalone-caller";
const CALLEE_ID: &str = "graph-standalone-callee";

#[test]
fn graph_commands_run_without_daemon_when_services_are_available() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping graph_standalone smoke; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

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
            "name": "graph-standalone",
            "created_at": "2026-05-28T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    seed_project(&mut conn);

    let sync = run_gcode(
        &env,
        project.path(),
        &["graph", "sync-file", "--file", TEST_FILE],
    );
    assert_success(sync, "graph sync-file");

    let overview = json_command(&env, project.path(), &["graph", "overview"]);
    assert!(
        overview["nodes"]
            .as_array()
            .is_some_and(|nodes| !nodes.is_empty())
    );

    let file = json_command(
        &env,
        project.path(),
        &["graph", "file", "--file", TEST_FILE],
    );
    assert!(
        file["links"]
            .as_array()
            .is_some_and(|links| !links.is_empty())
    );

    let neighbors = json_command(
        &env,
        project.path(),
        &[
            "graph",
            "neighbors",
            "--symbol-id",
            CALLER_ID,
            "--limit",
            "10",
        ],
    );
    assert!(
        neighbors["nodes"]
            .as_array()
            .is_some_and(|nodes| nodes.iter().any(|node| node["id"] == CALLEE_ID))
    );

    let blast_symbol = json_command(
        &env,
        project.path(),
        &[
            "graph",
            "blast-radius",
            "--symbol-id",
            CALLER_ID,
            "--depth",
            "2",
            "--limit",
            "10",
        ],
    );
    assert_eq!(blast_symbol["center"], CALLER_ID);

    let blast_file = json_command(
        &env,
        project.path(),
        &[
            "graph",
            "blast-radius",
            "--file",
            TEST_FILE,
            "--depth",
            "2",
            "--limit",
            "10",
        ],
    );
    assert_eq!(blast_file["center"], TEST_FILE);

    let clear = json_command(&env, project.path(), &["graph", "clear"]);
    assert_eq!(clear["success"], true);

    let rebuild = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuild["success"], true);
    assert_eq!(rebuild["files_processed"], 1);
    assert_eq!(rebuild["files_synced"], 1);

    cleanup_project(&mut conn);
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

fn run_gcode(env: &StandaloneEnv, cwd: &std::path::Path, args: &[&str]) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
    command
        .current_dir(cwd)
        .env("GCODE_DATABASE_URL", &env.database_url)
        .env("GOBBY_FALKORDB_HOST", &env.falkor_host)
        .env("GOBBY_FALKORDB_PORT", &env.falkor_port)
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

fn json_command(env: &StandaloneEnv, cwd: &std::path::Path, args: &[&str]) -> Value {
    let output = run_gcode(env, cwd, args);
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
    cleanup_project(conn);
    conn.batch_execute(
        "INSERT INTO code_indexed_projects
            (id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms)
         VALUES
            ('graph-standalone-project', '/tmp/graph-standalone', 1, 2, NOW(), 0);

         INSERT INTO code_indexed_files
            (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
             graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
         VALUES
            ('graph-standalone-file', 'graph-standalone-project', 'src/lib.rs', 'rust',
             'hash-1', 2, 54, false, true, NULL, NOW());

         INSERT INTO code_symbols
            (id, project_id, file_path, name, qualified_name, kind, language, byte_start, byte_end,
             line_start, line_end, signature, docstring, parent_symbol_id, content_hash,
             summary, created_at, updated_at)
         VALUES
            ('graph-standalone-caller', 'graph-standalone-project', 'src/lib.rs', 'caller',
             'crate::caller', 'function', 'rust', 0, 28, 1, 1, 'pub fn caller()', NULL, NULL,
             'hash-1', NULL, NOW(), NOW()),
            ('graph-standalone-callee', 'graph-standalone-project', 'src/lib.rs', 'callee',
             'crate::callee', 'function', 'rust', 29, 47, 2, 2, 'pub fn callee()', NULL, NULL,
             'hash-1', NULL, NOW(), NOW());

         INSERT INTO code_imports (project_id, source_file, target_module)
         VALUES ('graph-standalone-project', 'src/lib.rs', 'std');

         INSERT INTO code_calls
            (project_id, caller_symbol_id, callee_symbol_id, callee_name, callee_target_kind,
             callee_external_module, file_path, line)
         VALUES
            ('graph-standalone-project', 'graph-standalone-caller', 'graph-standalone-callee',
             'callee', 'symbol', '', 'src/lib.rs', 1);",
    )
    .expect("seed graph rows");
}

fn cleanup_project(conn: &mut Client) {
    conn.batch_execute(
        "DELETE FROM code_calls WHERE project_id = 'graph-standalone-project';
         DELETE FROM code_imports WHERE project_id = 'graph-standalone-project';
         DELETE FROM code_symbols WHERE project_id = 'graph-standalone-project';
         DELETE FROM code_content_chunks WHERE project_id = 'graph-standalone-project';
         DELETE FROM code_indexed_files WHERE project_id = 'graph-standalone-project';
         DELETE FROM code_indexed_projects WHERE id = 'graph-standalone-project';",
    )
    .expect("cleanup graph rows");
}

mod common;

use common::{ProjectCleanup, cleanup_project};
use postgres::{Client, NoTls};
use serde_json::Value;
use std::fs;
use std::process::{Command, Output};

const TEST_PROJECT_ID: &str = "graph-standalone-project";
const TEST_FILE: &str = "src/lib.rs";
const CONTENT_ONLY_FILE: &str = "docs/content.txt";
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
    fs::create_dir_all(project.path().join("docs")).expect("create docs");
    fs::write(
        project.path().join("src/lib.rs"),
        "pub fn caller() { callee(); }\npub fn callee() {}\n",
    )
    .expect("write source");
    fs::write(
        project.path().join(CONTENT_ONLY_FILE),
        "plain prose without code graph facts\n",
    )
    .expect("write content-only source");
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
    let _cleanup = ProjectCleanup::new(&env.database_url, TEST_PROJECT_ID);
    seed_project(&mut conn);

    let missing = run_gcode(
        &env,
        project.path(),
        &["graph", "sync-file", "--file", "src/missing.rs"],
    );
    assert_eq!(
        missing.status.code(),
        Some(2),
        "missing indexed file should exit 2\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&missing.stdout),
        String::from_utf8_lossy(&missing.stderr)
    );
    let missing_json: Value =
        serde_json::from_slice(&missing.stdout).expect("missing file emits JSON");
    assert_eq!(missing_json["reason"], "indexed_file_not_found");

    let skipped = json_command(
        &env,
        project.path(),
        &[
            "graph",
            "sync-file",
            "--file",
            "src/missing.rs",
            "--allow-missing-indexed-file",
        ],
    );
    assert_eq!(skipped["status"], "skipped");
    assert_eq!(skipped["reason"], "indexed_file_not_found");

    let content_skip = json_command(
        &env,
        project.path(),
        &["graph", "sync-file", "--file", CONTENT_ONLY_FILE],
    );
    assert_no_graph_facts_skip(&content_skip);
    assert!(graph_synced(&mut conn, CONTENT_ONLY_FILE));
    let overview_after_content_skip = json_command(&env, project.path(), &["graph", "overview"]);
    assert!(
        !overview_has_file(&overview_after_content_skip, CONTENT_ONLY_FILE),
        "content-only skip should not create a file node: {overview_after_content_skip}"
    );

    seed_temporary_content_import(&mut conn);
    let stale_seed = run_gcode(
        &env,
        project.path(),
        &["graph", "sync-file", "--file", CONTENT_ONLY_FILE],
    );
    assert_success(stale_seed, "seed stale content graph projection");
    let overview_with_stale = json_command(&env, project.path(), &["graph", "overview"]);
    assert!(
        overview_has_file(&overview_with_stale, CONTENT_ONLY_FILE),
        "temporary import sync should create a stale file node: {overview_with_stale}"
    );

    clear_temporary_content_import(&mut conn);
    let stale_cleanup = json_command(
        &env,
        project.path(),
        &["graph", "sync-file", "--file", CONTENT_ONLY_FILE],
    );
    assert_no_graph_facts_skip(&stale_cleanup);
    assert!(graph_synced(&mut conn, CONTENT_ONLY_FILE));
    let overview_after_cleanup = json_command(&env, project.path(), &["graph", "overview"]);
    assert!(
        !overview_has_file(&overview_after_cleanup, CONTENT_ONLY_FILE),
        "no-fact sync should remove stale file node: {overview_after_cleanup}"
    );

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
}

#[test]
fn graph_sync_file_classifies_missing_project_before_graph_access() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping graph sync-file missing-project contract; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("src")).expect("create src");
    fs::write(project.path().join("src/lib.rs"), "pub fn orphan() {}\n").expect("write source");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": "graph-missing-indexed-project",
            "name": "graph-missing-indexed-project",
            "created_at": "2026-05-28T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let _cleanup = ProjectCleanup::new(&env.database_url, "graph-missing-indexed-project");
    let output = run_gcode(
        &env,
        project.path(),
        &["graph", "sync-file", "--file", TEST_FILE],
    );
    assert_eq!(
        output.status.code(),
        Some(2),
        "missing indexed project should exit 2\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let payload: Value = serde_json::from_slice(&output.stdout).expect("missing project JSON");
    assert_eq!(payload["reason"], "project_not_indexed");
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

fn assert_no_graph_facts_skip(payload: &Value) {
    assert_eq!(payload["success"], true);
    assert_eq!(payload["status"], "skipped");
    assert_eq!(payload["reason"], "no_graph_facts");
    assert_eq!(payload["file_path"], CONTENT_ONLY_FILE);
    assert_eq!(payload["relationships_written"], 0);
    assert_eq!(payload["synced_files"], 1);
    assert_eq!(payload["synced_symbols"], 0);
}

fn overview_has_file(overview: &Value, file_path: &str) -> bool {
    overview["nodes"].as_array().is_some_and(|nodes| {
        nodes
            .iter()
            .any(|node| node["type"] == "file" && node["id"] == file_path)
    })
}

fn graph_synced(conn: &mut Client, file_path: &str) -> bool {
    conn.query_one(
        "SELECT graph_synced
         FROM code_indexed_files
         WHERE project_id = $1 AND file_path = $2",
        &[&TEST_PROJECT_ID, &file_path],
    )
    .expect("read graph_synced")
    .get(0)
}

fn seed_temporary_content_import(conn: &mut Client) {
    conn.execute(
        "INSERT INTO code_imports (project_id, source_file, target_module)
         VALUES ($1, $2, 'temporary.stale.module')",
        &[&TEST_PROJECT_ID, &CONTENT_ONLY_FILE],
    )
    .expect("insert temporary content import");
    conn.execute(
        "UPDATE code_indexed_files
         SET graph_synced = false, graph_sync_attempted_at = NULL
         WHERE project_id = $1 AND file_path = $2",
        &[&TEST_PROJECT_ID, &CONTENT_ONLY_FILE],
    )
    .expect("mark content file graph stale");
}

fn clear_temporary_content_import(conn: &mut Client) {
    conn.execute(
        "DELETE FROM code_imports
         WHERE project_id = $1 AND source_file = $2",
        &[&TEST_PROJECT_ID, &CONTENT_ONLY_FILE],
    )
    .expect("delete temporary content import");
    conn.execute(
        "UPDATE code_indexed_files
         SET graph_synced = false, graph_sync_attempted_at = NULL
         WHERE project_id = $1 AND file_path = $2",
        &[&TEST_PROJECT_ID, &CONTENT_ONLY_FILE],
    )
    .expect("mark content file graph stale after import removal");
}

fn seed_project(conn: &mut Client) {
    cleanup_project(conn, TEST_PROJECT_ID).expect("cleanup graph rows");
    conn.batch_execute(
        "INSERT INTO code_indexed_projects
            (id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms)
         VALUES
            ('graph-standalone-project', '/tmp/graph-standalone', 2, 2, NOW(), 0);

         INSERT INTO code_indexed_files
            (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
             graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
         VALUES
            ('graph-standalone-file', 'graph-standalone-project', 'src/lib.rs', 'rust',
             'hash-1', 2, 54, false, true, NULL, NOW()),
            ('graph-standalone-content-file', 'graph-standalone-project', 'docs/content.txt', 'text',
             'hash-content', 0, 35, false, true, NULL, NOW());

         INSERT INTO code_content_chunks
            (id, project_id, file_path, chunk_index, line_start, line_end, content, language,
             created_at)
         VALUES
            ('graph-standalone-content-chunk-0', 'graph-standalone-project', 'docs/content.txt',
             0, 1, 1, 'plain prose without code graph facts', 'text', NOW());

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

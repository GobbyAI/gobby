pub(super) use crate::common::{ProjectCleanup, cleanup_project};
pub(super) use gobby_code::graph::typed_query::string_params;
pub(super) use gobby_core::falkor::GraphClient;
pub(super) use postgres::{Client, NoTls};
pub(super) use serde_json::Value;
pub(super) use std::fs;
pub(super) use std::process::{Command, Output};

pub(super) const TEST_PROJECT_ID: &str = "graph-standalone-project";
pub(super) const LOCAL_IMPORT_PROJECT_ID: &str = "graph-standalone-local-import";
pub(super) const NO_PHANTOM_PROJECT_ID: &str = "graph-standalone-no-phantom";
pub(super) const GO_LOCAL_PROJECT_ID: &str = "graph-standalone-go-local";
pub(super) const CPP_LOCAL_PROJECT_ID: &str = "graph-standalone-cpp-local";
pub(super) const CPP_DB_LOCAL_PROJECT_ID: &str = "graph-standalone-cpp-db-local";
pub(super) const JAVA_LOCAL_PROJECT_ID: &str = "graph-standalone-java-local";
pub(super) const CSHARP_LOCAL_PROJECT_ID: &str = "graph-standalone-csharp-local";
pub(super) const KOTLIN_LOCAL_PROJECT_ID: &str = "graph-standalone-kotlin-local";
pub(super) const RUBY_LOCAL_PROJECT_ID: &str = "graph-standalone-ruby-local";
pub(super) const PHP_LOCAL_PROJECT_ID: &str = "graph-standalone-php-local";
pub(super) const SWIFT_LOCAL_PROJECT_ID: &str = "graph-standalone-swift-local";
pub(super) const DART_LOCAL_PROJECT_ID: &str = "graph-standalone-dart-local";
pub(super) const ELIXIR_LOCAL_PROJECT_ID: &str = "graph-standalone-elixir-local";
pub(super) const TEST_FILE: &str = "src/lib.rs";
pub(super) const CONTENT_ONLY_FILE: &str = "docs/content.txt";
pub(super) const CALLER_ID: &str = "graph-standalone-caller";
pub(super) const CALLEE_ID: &str = "graph-standalone-callee";

pub(super) struct StandaloneEnv {
    pub(super) database_url: String,
    pub(super) falkor_host: String,
    pub(super) falkor_port: String,
    pub(super) falkor_password: Option<String>,
}

impl StandaloneEnv {
    pub(super) fn from_env() -> Option<Self> {
        Some(Self {
            database_url: std::env::var("GCODE_GRAPH_STANDALONE_DATABASE_URL").ok()?,
            falkor_host: std::env::var("GCODE_GRAPH_STANDALONE_FALKOR_HOST").ok()?,
            falkor_port: std::env::var("GCODE_GRAPH_STANDALONE_FALKOR_PORT").ok()?,
            falkor_password: std::env::var("GCODE_GRAPH_STANDALONE_FALKOR_PASSWORD").ok(),
        })
    }
}

pub(super) fn run_gcode(env: &StandaloneEnv, cwd: &std::path::Path, args: &[&str]) -> Output {
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

pub(super) fn json_command(env: &StandaloneEnv, cwd: &std::path::Path, args: &[&str]) -> Value {
    let output = run_gcode(env, cwd, args);
    assert_success(output, &args.join(" "))
}

pub(super) fn assert_success(output: Output, label: &str) -> Value {
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

pub(super) fn assert_no_graph_facts_skip(payload: &Value) {
    assert_eq!(payload["success"], true);
    assert_eq!(payload["status"], "skipped");
    assert_eq!(payload["reason"], "no_graph_facts");
    assert_eq!(payload["file_path"], CONTENT_ONLY_FILE);
    assert_eq!(payload["relationships_written"], 0);
    assert_eq!(payload["synced_files"], 1);
    assert_eq!(payload["synced_symbols"], 0);
}

pub(super) fn overview_has_file(overview: &Value, file_path: &str) -> bool {
    overview["nodes"].as_array().is_some_and(|nodes| {
        nodes
            .iter()
            .any(|node| node["type"] == "file" && node["id"] == file_path)
    })
}

pub(super) fn graph_synced(conn: &mut Client, file_path: &str) -> bool {
    conn.query_one(
        "SELECT graph_synced
         FROM code_indexed_files
         WHERE project_id = $1 AND file_path = $2",
        &[&TEST_PROJECT_ID, &file_path],
    )
    .expect("read graph_synced")
    .get(0)
}

pub(super) fn seed_temporary_content_import(conn: &mut Client) {
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

pub(super) fn clear_temporary_content_import(conn: &mut Client) {
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

pub(super) fn seed_project(conn: &mut Client) {
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

/// Connect a read client to the shared code graph (project-scoped by node
/// property), reusing the standalone FalkorDB env.
pub(super) fn phantom_graph_client(env: &StandaloneEnv) -> GraphClient {
    let config = gobby_core::config::FalkorConfig {
        host: env.falkor_host.clone(),
        port: env.falkor_port.parse().expect("falkor port"),
        password: env.falkor_password.clone(),
    };
    GraphClient::from_config(&config, gobby_core::config::CODE_GRAPH_NAME)
        .expect("connect FalkorDB")
}

/// Count of `CALLS`-target `CodeSymbol` nodes with no incoming `DEFINES` edge —
/// i.e. phantom nodes. Must be zero after a clean projection.
pub(super) fn phantom_call_target_count(graph: &mut GraphClient, project_id: &str) -> i64 {
    let params = string_params(&[("project", project_id)]);
    let rows = graph
        .query(
            "MATCH ()-[:CALLS]->(s:CodeSymbol {project: $project})
             WHERE NOT (:CodeFile {project: $project})-[:DEFINES]->(s)
             RETURN count(DISTINCT s) AS phantoms",
            Some(params),
        )
        .expect("phantom count query");
    rows.first()
        .and_then(|row| row.get("phantoms"))
        .and_then(serde_json::Value::as_i64)
        .unwrap_or_else(|| panic!("expected a phantom count row: {rows:?}"))
}

/// Whether `symbol_id` is a `CodeSymbol` that is both defined (incoming
/// `DEFINES` from its `CodeFile`) and called (incoming `CALLS`).
pub(super) fn resolved_target_is_defined_and_called(
    graph: &mut GraphClient,
    project_id: &str,
    symbol_id: &str,
) -> bool {
    let params = string_params(&[("project", project_id), ("id", symbol_id)]);
    let rows = graph
        .query(
            "MATCH (:CodeFile {project: $project})-[:DEFINES]->(s:CodeSymbol {project: $project, id: $id})
             WHERE ()-[:CALLS]->(s)
             RETURN count(s) AS defined",
            Some(params),
        )
        .expect("defined-target query");
    rows.first()
        .and_then(|row| row.get("defined"))
        .and_then(serde_json::Value::as_i64)
        .unwrap_or(0)
        > 0
}

pub(super) fn assert_caller_present(
    env: &StandaloneEnv,
    cwd: &std::path::Path,
    target: &str,
    caller: &str,
    when: &str,
) {
    let callers = json_command(env, cwd, &["callers", target]);
    assert!(
        callers["total"].as_u64().is_some_and(|total| total >= 1),
        "expected a cross-file caller of `{target}` {when}: {callers}"
    );
    assert!(
        callers["results"]
            .as_array()
            .is_some_and(|results| results.iter().any(|result| result["name"] == caller)),
        "expected `{caller}` among callers of `{target}` {when}: {callers}"
    );
}

pub(super) fn assert_blast_radius_reports_affected_callers(blast: &Value) {
    let results = blast["results"]
        .as_array()
        .unwrap_or_else(|| panic!("blast-radius results must be an array: {blast}"));
    assert!(
        results.iter().any(|result| {
            result["id"].as_str().is_some()
                && result["distance"]
                    .as_i64()
                    .is_some_and(|distance| distance >= 1)
        }),
        "blast-radius should report affected callers: {blast}"
    );
}

pub(super) fn required_symbol_id(
    conn: &mut Client,
    project_id: &str,
    file_path: &str,
    name: &str,
) -> String {
    conn.query_one(
        "SELECT id FROM code_symbols WHERE project_id = $1 AND file_path = $2 AND name = $3",
        &[&project_id, &file_path, &name],
    )
    .unwrap_or_else(|err| panic!("symbol {file_path}:{name} not indexed: {err}"))
    .get::<_, String>(0)
}

pub(super) fn resolved_call_target(
    conn: &mut Client,
    project_id: &str,
    file_path: &str,
    callee_name: &str,
) -> Option<String> {
    let row = conn
        .query_opt(
            "SELECT callee_symbol_id, callee_target_kind FROM code_calls
             WHERE project_id = $1 AND file_path = $2 AND callee_name = $3",
            &[&project_id, &file_path, &callee_name],
        )
        .expect("read code_calls row")?;
    let kind: String = row.get("callee_target_kind");
    let callee_symbol_id: String = row.get("callee_symbol_id");
    (kind == "symbol" && !callee_symbol_id.is_empty()).then_some(callee_symbol_id)
}

pub(super) fn pending_local_import_count(conn: &mut Client, project_id: &str) -> i64 {
    conn.query_one(
        "SELECT COUNT(*) FROM code_calls
         WHERE project_id = $1 AND callee_target_kind = 'local_import'",
        &[&project_id],
    )
    .expect("count local_import rows")
    .get::<_, i64>(0)
}

pub(super) use crate::common::{ProjectCleanup, cleanup_project};
pub(super) use gobby_core::falkor::GraphClient;
pub(super) use postgres::{Client, NoTls};
pub(super) use serde_json::Value;
pub(super) use std::fs;
pub(super) use std::process::{Command, Output};

pub(super) fn string_params(values: &[(&str, &str)]) -> std::collections::HashMap<String, String> {
    values
        .iter()
        .map(|(key, value)| ((*key).to_string(), cypher_string_literal(value)))
        .collect()
}

fn cypher_string_literal(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '\\' => escaped.push_str("\\\\"),
            '\'' => escaped.push_str("\\'"),
            '"' => escaped.push_str("\\\""),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            '\u{0008}' => escaped.push_str("\\b"),
            '\u{000C}' => escaped.push_str("\\f"),
            ch if ch.is_control() => escaped.push_str(&format!("\\u{:04X}", ch as u32)),
            ch => escaped.push(ch),
        }
    }
    format!("'{escaped}'")
}

// Fixture ids are UUIDv5(CODE_INDEX_UUID_NAMESPACE, <legacy label>) because the
// hub stores every code_* id column as native uuid. The legacy label is kept in
// the trailing comment for greppability.
pub(super) const TEST_PROJECT_ID: &str = "285bdd28-41b4-5acb-878b-6e72cf276dd1"; // graph-standalone-project
pub(super) const HARNESS_MACHINE_ID: &str = "ff5dd0ce-20a1-5f6c-8a89-ea85c2bbeea9"; // graph-standalone-machine
pub(super) const MISSING_INDEXED_PROJECT_ID: &str = "8a20293a-76d6-559d-990d-df24b32cc0e2"; // graph-missing-indexed-project
pub(super) const LOCAL_IMPORT_PROJECT_ID: &str = "b75024f9-b106-5eac-86f1-f35805f97dbe"; // graph-standalone-local-import
pub(super) const NO_PHANTOM_PROJECT_ID: &str = "9865ea81-c9bb-5edc-8de6-895686366ae6"; // graph-standalone-no-phantom
pub(super) const GO_LOCAL_PROJECT_ID: &str = "acd54de2-7205-5fb0-a6c6-20570f333d26"; // graph-standalone-go-local
pub(super) const JS_DEFAULT_LOCAL_PROJECT_ID: &str = "3395e8e7-92ca-504f-876c-552a46f195c9"; // graph-standalone-js-default-local
pub(super) const CPP_LOCAL_PROJECT_ID: &str = "683a03e5-7426-5c46-8e5a-3044389e9e56"; // graph-standalone-cpp-local
pub(super) const JAVA_LOCAL_PROJECT_ID: &str = "75cc2ce2-af55-591b-93ad-376b7f1c1a81"; // graph-standalone-java-local
pub(super) const RUST_LOCAL_PROJECT_ID: &str = "6f9f3805-07fa-5a2f-ab94-b98cb174ce0e"; // graph-standalone-rust-local
pub(super) const RUST_TUPLE_LOCAL_PROJECT_ID: &str = "c34315ca-8611-52fb-a4fb-e273ccb73652"; // graph-standalone-rust-tuple-local
pub(super) const CSHARP_LOCAL_PROJECT_ID: &str = "622f74a4-d013-52d2-93e7-fa9ec0776e6a"; // graph-standalone-csharp-local
pub(super) const KOTLIN_LOCAL_PROJECT_ID: &str = "154cac87-2e35-52d1-806d-154211fa5612"; // graph-standalone-kotlin-local
pub(super) const RUBY_LOCAL_PROJECT_ID: &str = "6d1edeec-02cb-531e-a7eb-d930ff9d6f2f"; // graph-standalone-ruby-local
pub(super) const PHP_LOCAL_PROJECT_ID: &str = "d97f0d47-e417-54c6-b52b-3bc48100f30f"; // graph-standalone-php-local
pub(super) const SWIFT_LOCAL_PROJECT_ID: &str = "9f8b024f-9424-528d-9197-b096913810a4"; // graph-standalone-swift-local
pub(super) const DART_LOCAL_PROJECT_ID: &str = "7a9a47b9-49c9-5d3a-b06e-8da103312767"; // graph-standalone-dart-local
pub(super) const ELIXIR_LOCAL_PROJECT_ID: &str = "5683049f-7e07-5479-94f0-4cdf7efeace1"; // graph-standalone-elixir-local
pub(super) const BASH_LOCAL_PROJECT_ID: &str = "673400f5-f379-5763-bac1-87d48039dc4c"; // graph-standalone-bash-local
pub(super) const SCALA_LOCAL_PROJECT_ID: &str = "e633c8d2-5b69-58c4-b90d-33cad75e61d1"; // graph-standalone-scala-local
pub(super) const LUA_LOCAL_PROJECT_ID: &str = "2e331312-0310-5f3f-bad2-29a7c9560428"; // graph-standalone-lua-local
pub(super) const OBJC_LOCAL_PROJECT_ID: &str = "da1e1c4b-7fe3-50c8-9d00-ee9a4716718f"; // graph-standalone-objc-local
pub(super) const TEST_FILE: &str = "src/lib.rs";
pub(super) const CONTENT_ONLY_FILE: &str = "docs/content.txt";
pub(super) const CROSS_CRATE_CALLER_FILE: &str = "crates/app/src/lib.rs";
pub(super) const CROSS_CRATE_CALLEE_FILE: &str = "crates/core/src/lib.rs";
pub(super) const CALLER_ID: &str = "47fc1394-cd88-52c2-a32b-81f7d4d03ec8"; // graph-standalone-caller
pub(super) const CALLEE_ID: &str = "63dfaaf8-2110-5dd8-859b-43f89fbf8c0e"; // graph-standalone-callee
pub(super) const CROSS_CRATE_CALLER_ID: &str = "1507e898-6103-5a15-83b7-6ac30bd1305e"; // graph-standalone-cross-crate-caller
pub(super) const CROSS_CRATE_CALLEE_ID: &str = "5b150a61-ddd2-59b8-99d1-fcc3d08975c6"; // graph-standalone-cross-crate-callee
const TEST_FILE_ID: &str = "e5faf0b1-769c-552a-a427-6b59e645ce0f"; // graph-standalone-file
const CONTENT_FILE_ID: &str = "455cbd58-c740-585a-aa3a-165d0b798b48"; // graph-standalone-content-file
const CROSS_CRATE_CALLER_FILE_ID: &str = "3df4d985-06e2-59bb-a3fb-ff77d812ff08"; // graph-standalone-cross-crate-caller-file
const CROSS_CRATE_CALLEE_FILE_ID: &str = "74950bb1-1175-5dcf-844c-91566f0cc73c"; // graph-standalone-cross-crate-callee-file
const CONTENT_CHUNK_ID: &str = "1b1e54a0-b042-5de5-8b45-3ed12b15a701"; // graph-standalone-content-chunk-0

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
    run_gcode_with_format(env, cwd, "json", args)
}

pub(super) fn run_gcode_with_format(
    env: &StandaloneEnv,
    cwd: &std::path::Path,
    format: &str,
    args: &[&str],
) -> Output {
    // gcode resolves local machine identity from $GOBBY_HOME/machine_id;
    // provision the daemon-free home with the shared harness identity so
    // machine-scoped index state resolves consistently across the suite.
    let gobby_home = cwd.join(".no-daemon-home");
    fs::create_dir_all(&gobby_home).expect("create no-daemon home");
    let machine_id_path = gobby_home.join("machine_id");
    if !machine_id_path.exists() {
        fs::write(&machine_id_path, HARNESS_MACHINE_ID).expect("write harness machine id");
    }
    let project_id = project_id_for(cwd);
    let port = env.falkor_port.parse::<i64>().unwrap_or(6379);
    let connections = gobby_core::grant::DirectConnections::postgres(&env.database_url)
        .with_falkor(&env.falkor_host, port, env.falkor_password.as_deref());
    let grant =
        gobby_core::grant::managed_direct_grant(&project_id, HARNESS_MACHINE_ID, &connections);
    let grant_path = gobby_core::grant::write_managed_bootstrap(&gobby_home.join("grants"), &grant)
        .expect("write managed grant");
    let mut command = Command::new(env!("CARGO_BIN_EXE_gcode"));
    command
        .current_dir(cwd)
        .env("GOBBY_HOME", &gobby_home)
        .env("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", grant_path)
        .arg("--allow-stale")
        .arg("--format")
        .arg(format)
        .args(args);
    command.output().expect("run gcode")
}

fn project_id_for(cwd: &std::path::Path) -> String {
    gobby_core::project::read_project_id(cwd).unwrap_or_else(|_| TEST_PROJECT_ID.to_string())
}

#[test]
fn project_id_for_reads_project_json_before_fallback() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let gobby = tmp.path().join(".gobby");
    fs::create_dir_all(&gobby).expect("create .gobby");
    fs::write(
        gobby.join("project.json"),
        r#"{"id":"11111111-1111-4111-8111-111111111111","name":"only-project"}"#,
    )
    .expect("write project.json");
    assert_eq!(
        project_id_for(tmp.path()),
        "11111111-1111-4111-8111-111111111111"
    );
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
        &[&crate::common::uuid_param(TEST_PROJECT_ID), &file_path],
    )
    .expect("read graph_synced")
    .get(0)
}

pub(super) fn seed_temporary_content_import(conn: &mut Client) {
    let project_id = crate::common::uuid_param(TEST_PROJECT_ID);
    conn.execute(
        "INSERT INTO code_imports (project_id, source_file, content_hash, target_module)
         VALUES ($1, $2, 'hash-content', 'temporary.stale.module')",
        &[&project_id, &CONTENT_ONLY_FILE],
    )
    .expect("insert temporary content import");
    conn.execute(
        "UPDATE code_indexed_files
         SET graph_synced = false, graph_sync_attempted_at = NULL
         WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &CONTENT_ONLY_FILE],
    )
    .expect("mark content file graph stale");
}

pub(super) fn clear_temporary_content_import(conn: &mut Client) {
    let project_id = crate::common::uuid_param(TEST_PROJECT_ID);
    conn.execute(
        "DELETE FROM code_imports
         WHERE project_id = $1 AND source_file = $2",
        &[&project_id, &CONTENT_ONLY_FILE],
    )
    .expect("delete temporary content import");
    conn.execute(
        "UPDATE code_indexed_files
         SET graph_synced = false, graph_sync_attempted_at = NULL
         WHERE project_id = $1 AND file_path = $2",
        &[&project_id, &CONTENT_ONLY_FILE],
    )
    .expect("mark content file graph stale after import removal");
}

pub(super) fn seed_project(conn: &mut Client) {
    cleanup_project(conn, TEST_PROJECT_ID).expect("cleanup graph rows");
    conn.batch_execute(&format!(
        "INSERT INTO code_indexed_projects (id)
         VALUES ('{TEST_PROJECT_ID}');

         INSERT INTO code_indexed_project_states
            (machine_id, project_id, root_path, total_files, total_symbols, last_indexed_at,
             index_duration_ms)
         VALUES
            ('{HARNESS_MACHINE_ID}', '{TEST_PROJECT_ID}', '/tmp/graph-standalone', 4, 4, NOW(),
             0);

         INSERT INTO code_indexed_files
            (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
             graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
         VALUES
            ('{TEST_FILE_ID}', '{TEST_PROJECT_ID}', 'src/lib.rs', 'rust',
             'hash-1', 2, 54, false, true, NULL, NOW()),
            ('{CONTENT_FILE_ID}', '{TEST_PROJECT_ID}', 'docs/content.txt', 'text',
             'hash-content', 0, 35, false, true, NULL, NOW()),
            ('{CROSS_CRATE_CALLER_FILE_ID}', '{TEST_PROJECT_ID}',
             'crates/app/src/lib.rs', 'rust', 'hash-cross-caller', 1, 38, false, true, NULL,
             NOW()),
            ('{CROSS_CRATE_CALLEE_FILE_ID}', '{TEST_PROJECT_ID}',
             'crates/core/src/lib.rs', 'rust', 'hash-cross-callee', 1, 24, false, true, NULL,
             NOW());

         INSERT INTO code_indexed_file_states
            (machine_id, project_id, file_path, content_hash)
         VALUES
            ('{HARNESS_MACHINE_ID}', '{TEST_PROJECT_ID}', 'src/lib.rs', 'hash-1'),
            ('{HARNESS_MACHINE_ID}', '{TEST_PROJECT_ID}', 'docs/content.txt', 'hash-content'),
            ('{HARNESS_MACHINE_ID}', '{TEST_PROJECT_ID}', 'crates/app/src/lib.rs',
             'hash-cross-caller'),
            ('{HARNESS_MACHINE_ID}', '{TEST_PROJECT_ID}', 'crates/core/src/lib.rs',
             'hash-cross-callee');

         INSERT INTO code_content_chunks
            (id, project_id, file_path, content_hash, chunk_index, line_start, line_end, content,
             language, created_at)
         VALUES
            ('{CONTENT_CHUNK_ID}', '{TEST_PROJECT_ID}', 'docs/content.txt', 'hash-content',
             0, 1, 1, 'plain prose without code graph facts', 'text', NOW());

         INSERT INTO code_symbols
            (id, project_id, file_path, name, qualified_name, kind, language, byte_start, byte_end,
             line_start, line_end, signature, docstring, parent_symbol_id, file_content_hash,
             content_hash, summary, created_at, updated_at)
         VALUES
            ('{CALLER_ID}', '{TEST_PROJECT_ID}', 'src/lib.rs', 'caller',
             'crate::caller', 'function', 'rust', 0, 28, 1, 1, 'pub fn caller()', NULL, NULL,
             'hash-1', 'hash-sym-caller', NULL, NOW(), NOW()),
            ('{CALLEE_ID}', '{TEST_PROJECT_ID}', 'src/lib.rs', 'callee',
             'crate::callee', 'function', 'rust', 29, 47, 2, 2, 'pub fn callee()', NULL, NULL,
             'hash-1', 'hash-sym-callee', NULL, NOW(), NOW()),
            ('{CROSS_CRATE_CALLER_ID}', '{TEST_PROJECT_ID}',
             'crates/app/src/lib.rs', 'app_entry', 'app::app_entry', 'function', 'rust', 0, 38,
             1, 3, 'pub fn app_entry()', NULL, NULL, 'hash-cross-caller', 'hash-sym-app-entry',
             NULL, NOW(), NOW()),
            ('{CROSS_CRATE_CALLEE_ID}', '{TEST_PROJECT_ID}',
             'crates/core/src/lib.rs', 'core_leaf', 'core::core_leaf', 'function', 'rust', 0, 24,
             1, 1, 'pub fn core_leaf()', NULL, NULL, 'hash-cross-callee', 'hash-sym-core-leaf',
             NULL, NOW(), NOW());

         INSERT INTO code_imports (project_id, source_file, content_hash, target_module)
         VALUES ('{TEST_PROJECT_ID}', 'src/lib.rs', 'hash-1', 'std');

         INSERT INTO code_calls
            (project_id, caller_symbol_id, callee_symbol_id, callee_name, callee_target_kind,
             callee_external_module, file_path, content_hash, line)
        VALUES
            ('{TEST_PROJECT_ID}', '{CALLER_ID}', '{CALLEE_ID}',
             'callee', 'symbol', '', 'src/lib.rs', 'hash-1', 1),
            ('{TEST_PROJECT_ID}', '{CROSS_CRATE_CALLER_ID}',
             '{CROSS_CRATE_CALLEE_ID}', 'core_leaf', 'symbol', '',
             'crates/app/src/lib.rs', 'hash-cross-caller', 2);"
    ))
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
        &[&crate::common::uuid_param(project_id), &file_path, &name],
    )
    .unwrap_or_else(|err| panic!("symbol {file_path}:{name} not indexed: {err}"))
    .get::<_, uuid::Uuid>(0)
    .to_string()
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
            &[
                &crate::common::uuid_param(project_id),
                &file_path,
                &callee_name,
            ],
        )
        .expect("read code_calls row")?;
    let kind: String = row.get("callee_target_kind");
    let callee_symbol_id: Option<uuid::Uuid> = row.get("callee_symbol_id");
    match callee_symbol_id {
        Some(callee_symbol_id) if kind == "symbol" => Some(callee_symbol_id.to_string()),
        _ => None,
    }
}

pub(super) fn pending_local_import_count(conn: &mut Client, project_id: &str) -> i64 {
    conn.query_one(
        "SELECT COUNT(*) FROM code_calls
         WHERE project_id = $1 AND callee_target_kind = 'local_import'",
        &[&crate::common::uuid_param(project_id)],
    )
    .expect("count local_import rows")
    .get::<_, i64>(0)
}

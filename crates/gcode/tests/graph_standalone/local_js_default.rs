use super::support::*;

fn write_identity(project: &std::path::Path, project_id: &str, name: &str) {
    fs::write(
        project.join(".gobby/gcode.json"),
        serde_json::json!({
            "id": project_id,
            "name": name,
            "created_at": "2026-06-16T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");
}

fn seed_leftover_local_import(conn: &mut Client, caller_id: &str) {
    conn.execute(
        "INSERT INTO code_calls
            (project_id, caller_symbol_id, callee_symbol_id, callee_name, callee_target_kind,
             callee_external_module, file_path, line)
         VALUES ($1, $2, '', 'helper', 'local_import', 'src/utils.js', 'legacy.js', 99)",
        &[&JS_DEFAULT_LOCAL_PROJECT_ID, &caller_id],
    )
    .expect("seed leftover local_import row");
}

#[test]
fn index_resolves_local_javascript_default_imports_and_leftover_sweep() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping JavaScript default local-import resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("src")).expect("create src");
    fs::write(
        project.path().join("src/utils.js"),
        "export default function helper() {\n  return 42;\n}\n",
    )
    .expect("write utils.js");
    fs::write(
        project.path().join("src/main.js"),
        "import runHelper from './utils';\n\nexport function caller() {\n  return runHelper();\n}\n",
    )
    .expect("write main.js");
    write_identity(
        project.path(),
        JS_DEFAULT_LOCAL_PROJECT_ID,
        "graph-standalone-js-default-local",
    );

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    cleanup_project(&mut conn, JS_DEFAULT_LOCAL_PROJECT_ID).expect("cleanup JS default project");
    let _cleanup = ProjectCleanup::new(&env.database_url, JS_DEFAULT_LOCAL_PROJECT_ID);

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    let helper_id = required_symbol_id(
        &mut conn,
        JS_DEFAULT_LOCAL_PROJECT_ID,
        "src/utils.js",
        "helper",
    );
    let caller_id = required_symbol_id(
        &mut conn,
        JS_DEFAULT_LOCAL_PROJECT_ID,
        "src/main.js",
        "caller",
    );
    assert_eq!(
        resolved_call_target(
            &mut conn,
            JS_DEFAULT_LOCAL_PROJECT_ID,
            "src/main.js",
            "runHelper"
        ),
        Some(helper_id.clone()),
        "JavaScript default import call did not resolve to the canonical helper symbol"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, JS_DEFAULT_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive the first completed index run"
    );

    seed_leftover_local_import(&mut conn, &caller_id);
    assert_eq!(
        pending_local_import_count(&mut conn, JS_DEFAULT_LOCAL_PROJECT_ID),
        1,
        "seeded leftover local_import row should be pending before the sweep"
    );

    let reindexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(reindexed, "gcode index --full after leftover seed");
    assert_eq!(
        resolved_call_target(
            &mut conn,
            JS_DEFAULT_LOCAL_PROJECT_ID,
            "legacy.js",
            "helper"
        ),
        Some(helper_id),
        "full-index project sweep did not promote the leftover local_import row"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, JS_DEFAULT_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive the project-scoped sweep"
    );

    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "helper", "caller", "after rebuild");
}

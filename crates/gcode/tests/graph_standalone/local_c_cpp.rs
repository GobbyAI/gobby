use super::support::*;
use gobby_code::db;

#[test]
fn index_resolves_cross_file_local_cpp_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-file C++ local-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    let include_dir = project.path().join("include");
    let src_dir = project.path().join("src");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(&include_dir).expect("create include");
    fs::create_dir_all(&src_dir).expect("create src");

    let helper_path = include_dir.join("helper.hpp");
    let main_path = src_dir.join("main.cpp");
    fs::write(
        &helper_path,
        "#pragma once\n\ninline int helper() {\n    return 41;\n}\n",
    )
    .expect("write helper.hpp");
    fs::write(
        &main_path,
        "#include \"helper.hpp\"\n\nint run() {\n    return helper();\n}\n",
    )
    .expect("write main.cpp");
    fs::write(
        project.path().join("compile_commands.json"),
        serde_json::json!([{
            "directory": project.path().to_string_lossy(),
            "command": format!(
                "c++ -std=c++17 -I{} -c {}",
                include_dir.display(),
                main_path.display()
            ),
            "file": main_path.to_string_lossy()
        }])
        .to_string(),
    )
    .expect("write compile_commands.json");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": CPP_LOCAL_PROJECT_ID,
            "name": "graph-standalone-cpp-local",
            "created_at": "2026-06-16T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    cleanup_project(&mut conn, CPP_LOCAL_PROJECT_ID).expect("cleanup C++ project");
    let _cleanup = ProjectCleanup::new(&env.database_url, CPP_LOCAL_PROJECT_ID);

    let indexed = run_gcode(
        &env,
        project.path(),
        &["index", "--full", "--require-cpp-semantics"],
    );
    if cpp_semantics_unavailable(&indexed) {
        eprintln!(
            "skipping cross-file C++ local-call resolution; install clangd or set GCODE_CLANGD"
        );
        return;
    }
    assert_success(indexed, "gcode index --full --require-cpp-semantics");

    let helper_id = required_symbol_id(
        &mut conn,
        CPP_LOCAL_PROJECT_ID,
        "include/helper.hpp",
        "helper",
    );
    assert_eq!(
        resolved_call_target(&mut conn, CPP_LOCAL_PROJECT_ID, "src/main.cpp", "helper"),
        Some(helper_id.clone()),
        "cross-file C++ call did not resolve to the canonical helper symbol"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, CPP_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "helper", "run", "after rebuild");

    let mut graph = phantom_graph_client(&env);
    assert_eq!(
        phantom_call_target_count(&mut graph, CPP_LOCAL_PROJECT_ID),
        0,
        "no CALLS target may lack a DEFINES edge after rebuild"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, CPP_LOCAL_PROJECT_ID, &helper_id),
        "the resolved C++ target must be a defined, called CodeSymbol"
    );

    let blast = json_command(&env, project.path(), &["blast-radius", "helper"]);
    assert_blast_radius_reports_affected_callers(&blast);

    let sync = run_gcode(
        &env,
        project.path(),
        &["graph", "sync-file", "--file", "src/main.cpp"],
    );
    assert_success(sync, "graph sync-file src/main.cpp");
    assert_caller_present(&env, project.path(), "helper", "run", "after sync-file");
    assert_eq!(
        phantom_call_target_count(&mut graph, CPP_LOCAL_PROJECT_ID),
        0,
        "sync-file must not introduce a phantom CALLS target"
    );
}

#[test]
fn db_resolves_seeded_cpp_candidate_file_to_symbol_id() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping seeded C++ local-callee DB resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    cleanup_project(&mut conn, CPP_DB_LOCAL_PROJECT_ID).expect("cleanup C++ DB project");
    let _cleanup = ProjectCleanup::new(&env.database_url, CPP_DB_LOCAL_PROJECT_ID);
    conn.batch_execute(
        "INSERT INTO code_indexed_projects
            (id, root_path, total_files, total_symbols, last_indexed_at, index_duration_ms)
         VALUES
            ('graph-standalone-cpp-db-local', '/tmp/graph-standalone-cpp-db', 1, 1, NOW(), 0);

         INSERT INTO code_indexed_files
            (id, project_id, file_path, language, content_hash, symbol_count, byte_size,
             graph_synced, vectors_synced, graph_sync_attempted_at, indexed_at)
         VALUES
            ('graph-standalone-cpp-db-helper-file', 'graph-standalone-cpp-db-local',
             'include/helper.hpp', 'cpp', 'hash-cpp-helper', 1, 53, false, true, NULL, NOW());

         INSERT INTO code_symbols
            (id, project_id, file_path, name, qualified_name, kind, language, byte_start, byte_end,
             line_start, line_end, signature, docstring, parent_symbol_id, content_hash,
             summary, created_at, updated_at)
         VALUES
            ('graph-standalone-cpp-db-helper', 'graph-standalone-cpp-db-local',
             'include/helper.hpp', 'helper', 'helper', 'function', 'cpp', 14, 52,
             3, 5, 'inline int helper()', NULL, NULL, 'hash-cpp-helper', NULL, NOW(), NOW());",
    )
    .expect("seed C++ DB rows");

    let target_files = vec!["include/helper.hpp".to_string()];
    let resolved = db::resolve_local_callee_symbol_id(
        &mut conn,
        CPP_DB_LOCAL_PROJECT_ID,
        &target_files,
        "helper",
    )
    .expect("resolve seeded C++ local callee");
    assert_eq!(
        resolved.as_deref(),
        Some("graph-standalone-cpp-db-helper"),
        "seeded C++ candidate file should resolve to the indexed symbol id"
    );
}

fn cpp_semantics_unavailable(output: &Output) -> bool {
    if output.status.success() {
        return false;
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    stderr.contains("C/C++ semantic indexing requires clangd")
}

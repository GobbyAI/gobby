use super::support::*;

#[test]
fn index_resolves_bare_rust_module_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping bare Rust module-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("src")).expect("create src");
    fs::write(
        project.path().join("Cargo.toml"),
        "[package]\nname = \"rust-local-fixture\"\nversion = \"0.1.0\"\nedition = \"2021\"\n",
    )
    .expect("write Cargo.toml");
    fs::write(project.path().join("src/foo.rs"), "pub fn bar() {}\n").expect("write foo.rs");
    fs::write(
        project.path().join("src/main.rs"),
        "mod foo;\n\nfn caller() {\n    foo::bar();\n}\n",
    )
    .expect("write main.rs");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": RUST_LOCAL_PROJECT_ID,
            "name": "graph-standalone-rust-local",
            "created_at": "2026-06-16T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    cleanup_project(&mut conn, RUST_LOCAL_PROJECT_ID).expect("cleanup Rust project");
    let _cleanup = ProjectCleanup::new(&env.database_url, RUST_LOCAL_PROJECT_ID);

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    let bar_id = required_symbol_id(&mut conn, RUST_LOCAL_PROJECT_ID, "src/foo.rs", "bar");
    assert_eq!(
        resolved_call_target(&mut conn, RUST_LOCAL_PROJECT_ID, "src/main.rs", "bar"),
        Some(bar_id.clone()),
        "bare Rust module call did not resolve to the canonical function"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, RUST_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "bar", "caller", "after rebuild");

    let mut graph = phantom_graph_client(&env);
    assert_eq!(
        phantom_call_target_count(&mut graph, RUST_LOCAL_PROJECT_ID),
        0,
        "no CALLS target may lack a DEFINES edge after rebuild"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, RUST_LOCAL_PROJECT_ID, &bar_id),
        "the resolved Rust target must be a defined, called CodeSymbol"
    );

    let blast = json_command(&env, project.path(), &["blast-radius", "bar"]);
    assert_blast_radius_reports_affected_callers(&blast);

    let sync = run_gcode(
        &env,
        project.path(),
        &["graph", "sync-file", "--file", "src/main.rs"],
    );
    assert_success(sync, "graph sync-file src/main.rs");
    assert_caller_present(&env, project.path(), "bar", "caller", "after sync-file");
    assert_eq!(
        phantom_call_target_count(&mut graph, RUST_LOCAL_PROJECT_ID),
        0,
        "sync-file must not introduce a phantom CALLS target"
    );
}

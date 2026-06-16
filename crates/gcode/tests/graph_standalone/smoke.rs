use super::support::*;

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
    fs::create_dir_all(project.path().join("crates/app/src")).expect("create app crate src");
    fs::create_dir_all(project.path().join("crates/core/src")).expect("create core crate src");
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
        project.path().join(CROSS_CRATE_CALLER_FILE),
        "pub fn app_entry() {\n    core_leaf();\n}\n",
    )
    .expect("write app crate source");
    fs::write(
        project.path().join(CROSS_CRATE_CALLEE_FILE),
        "pub fn core_leaf() {}\n",
    )
    .expect("write core crate source");
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

    let path = json_command(&env, project.path(), &["path", "caller", "callee"]);
    assert_eq!(path["found"], true);
    assert_eq!(path["hops"], 1);
    let path_steps = path["path"]
        .as_array()
        .unwrap_or_else(|| panic!("path steps must be an array: {path}"));
    assert_eq!(path_steps.len(), 2);
    assert_eq!(path_steps[0]["name"], "caller");
    assert_eq!(path_steps[0]["file_path"], TEST_FILE);
    assert_eq!(path_steps[0]["line"], 1);
    assert_eq!(path_steps[1]["name"], "callee");
    assert_eq!(path_steps[1]["file_path"], TEST_FILE);
    assert_eq!(path_steps[1]["line"], 2);

    let no_path = json_command(&env, project.path(), &["path", "callee", "caller"]);
    assert_eq!(no_path["found"], false);
    assert!(
        no_path["path"]
            .as_array()
            .is_some_and(|steps| steps.is_empty()),
        "reverse path should be empty: {no_path}"
    );

    let path_text =
        run_gcode_with_format(&env, project.path(), "text", &["path", "caller", "callee"]);
    assert!(
        path_text.status.success(),
        "text path failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&path_text.stdout),
        String::from_utf8_lossy(&path_text.stderr)
    );
    let path_text_stdout = String::from_utf8_lossy(&path_text.stdout);
    assert!(path_text_stdout.contains("Shortest path from 'caller' to 'callee'"));
    assert!(path_text_stdout.contains("1. caller (src/lib.rs:1)"));
    assert!(path_text_stdout.contains("2. callee (src/lib.rs:2)"));

    let clear = json_command(&env, project.path(), &["graph", "clear"]);
    assert_eq!(clear["success"], true);

    let rebuild = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuild["success"], true);
    assert_eq!(rebuild["files_processed"], 4);
    assert_eq!(rebuild["files_synced"], 4);
    let overview_after_rebuild = json_command(&env, project.path(), &["graph", "overview"]);
    assert!(
        !overview_has_file(&overview_after_rebuild, CONTENT_ONLY_FILE),
        "rebuild should sync content-only files without creating graph nodes: {overview_after_rebuild}"
    );

    let cross_crate_path = json_command(&env, project.path(), &["path", "app_entry", "core_leaf"]);
    assert_eq!(cross_crate_path["found"], true);
    assert_eq!(cross_crate_path["hops"], 1);
    let cross_crate_steps = cross_crate_path["path"]
        .as_array()
        .unwrap_or_else(|| panic!("cross-crate path steps must be an array: {cross_crate_path}"));
    assert_eq!(cross_crate_steps.len(), 2);
    assert_eq!(cross_crate_steps[0]["id"], CROSS_CRATE_CALLER_ID);
    assert_eq!(cross_crate_steps[0]["file_path"], CROSS_CRATE_CALLER_FILE);
    assert_eq!(cross_crate_steps[1]["id"], CROSS_CRATE_CALLEE_ID);
    assert_eq!(cross_crate_steps[1]["file_path"], CROSS_CRATE_CALLEE_FILE);

    let cleanup = json_command(&env, project.path(), &["graph", "cleanup-orphans"]);
    assert_eq!(cleanup["status"], "ok");
    assert_eq!(cleanup["stale_graph_files_deleted"], 0);
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

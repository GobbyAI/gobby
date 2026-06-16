use super::support::*;

#[test]
fn index_resolves_cross_file_local_python_imports() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-file local-import resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::write(
        project.path().join("a.py"),
        "def recluster_project_entities():\n    pass\n\n\nclass Service:\n    pass\n",
    )
    .expect("write a.py");
    fs::write(
        project.path().join("b.py"),
        "from a import recluster_project_entities\n\
         from a import Service as Svc\n\n\n\
         def caller():\n    recluster_project_entities()\n    Svc()\n",
    )
    .expect("write b.py");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": LOCAL_IMPORT_PROJECT_ID,
            "name": "graph-standalone-local-import",
            "created_at": "2026-06-15T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = ProjectCleanup::new(&env.database_url, LOCAL_IMPORT_PROJECT_ID);

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    // Canonical target ids defined in a.py.
    let recluster_id = required_symbol_id(
        &mut conn,
        LOCAL_IMPORT_PROJECT_ID,
        "a.py",
        "recluster_project_entities",
    );
    let service_id = required_symbol_id(&mut conn, LOCAL_IMPORT_PROJECT_ID, "a.py", "Service");

    // b.py's cross-file function call and aliased-constructor call resolved to
    // the real indexed ids (the alias `Svc` resolves back to `Service`).
    assert_eq!(
        resolved_call_target(
            &mut conn,
            LOCAL_IMPORT_PROJECT_ID,
            "b.py",
            "recluster_project_entities"
        ),
        Some(recluster_id),
        "cross-file function call did not resolve to the canonical symbol"
    );
    assert_eq!(
        resolved_call_target(&mut conn, LOCAL_IMPORT_PROJECT_ID, "b.py", "Service"),
        Some(service_id),
        "aliased cross-file constructor did not resolve to the class symbol"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, LOCAL_IMPORT_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    // Projection path 1+2: rebuild from the resolved PostgreSQL facts, then the
    // cross-file caller must be reachable via callers and blast-radius.
    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(
        &env,
        project.path(),
        "recluster_project_entities",
        "caller",
        "after rebuild",
    );

    let blast = json_command(
        &env,
        project.path(),
        &["blast-radius", "recluster_project_entities"],
    );
    assert!(
        blast.get("center").is_some(),
        "blast-radius should report a center: {blast}"
    );

    // Projection path 3: sync-file must recreate the same canonical edge.
    let sync = run_gcode(
        &env,
        project.path(),
        &["graph", "sync-file", "--file", "b.py"],
    );
    assert_success(sync, "graph sync-file b.py");
    assert_caller_present(
        &env,
        project.path(),
        "recluster_project_entities",
        "caller",
        "after sync-file",
    );
}

/// #792: a Go selector call `pkg.Fn()` into another local (same-module) package
/// must resolve to the canonical symbol so `callers`/`blast-radius` include the
/// caller. Go packages are directory-granular, so the target may live in any
/// file of the package directory — here `Load` lives in `query.go`, not the
/// eponymous `store.go`. Exercises the import path → package directory mapping
/// across the index, rebuild, and sync-file projection paths.
#[test]
fn index_resolves_cross_package_local_go_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-package Go local-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("pkg/store")).expect("create pkg/store");
    fs::create_dir_all(project.path().join("cmd/app")).expect("create cmd/app");
    fs::write(project.path().join("go.mod"), "module example.com/app\n").expect("write go.mod");
    // Package `store` spans two files; the call target lives in the
    // non-eponymous one to prove package-granular (directory) resolution.
    fs::write(
        project.path().join("pkg/store/store.go"),
        "package store\n\ntype Record struct{}\n",
    )
    .expect("write store.go");
    fs::write(
        project.path().join("pkg/store/query.go"),
        "package store\n\nfunc Load() {}\n",
    )
    .expect("write query.go");
    fs::write(
        project.path().join("cmd/app/main.go"),
        "package main\n\nimport \"example.com/app/pkg/store\"\n\nfunc Run() {\n    store.Load()\n}\n",
    )
    .expect("write main.go");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": GO_LOCAL_PROJECT_ID,
            "name": "graph-standalone-go-local",
            "created_at": "2026-06-15T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = ProjectCleanup::new(&env.database_url, GO_LOCAL_PROJECT_ID);

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    // Canonical target id lives in the non-eponymous package file.
    let load_id = required_symbol_id(&mut conn, GO_LOCAL_PROJECT_ID, "pkg/store/query.go", "Load");

    // main.go's cross-package selector call resolved to the real indexed id
    // even though `Load` is not in the package's eponymous file.
    assert_eq!(
        resolved_call_target(&mut conn, GO_LOCAL_PROJECT_ID, "cmd/app/main.go", "Load"),
        Some(load_id.clone()),
        "cross-package Go selector call did not resolve to the canonical symbol"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, GO_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    // Projection path 1+2: rebuild from resolved PostgreSQL facts, then the
    // cross-package caller must be reachable via callers and the target must be
    // a real DEFINES-linked node (no phantom CALLS target).
    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "Load", "Run", "after rebuild");

    let mut graph = phantom_graph_client(&env);
    assert_eq!(
        phantom_call_target_count(&mut graph, GO_LOCAL_PROJECT_ID),
        0,
        "no CALLS target may lack a DEFINES edge after rebuild"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, GO_LOCAL_PROJECT_ID, &load_id),
        "the resolved Go target must be a defined, called CodeSymbol"
    );

    let blast = json_command(&env, project.path(), &["blast-radius", "Load"]);
    assert!(
        blast.get("center").is_some(),
        "blast-radius should report a center: {blast}"
    );

    // Projection path 3: sync-file must recreate the same canonical edge.
    let sync = run_gcode(
        &env,
        project.path(),
        &["graph", "sync-file", "--file", "cmd/app/main.go"],
    );
    assert_success(sync, "graph sync-file cmd/app/main.go");
    assert_caller_present(&env, project.path(), "Load", "Run", "after sync-file");
    assert_eq!(
        phantom_call_target_count(&mut graph, GO_LOCAL_PROJECT_ID),
        0,
        "sync-file must not introduce a phantom CALLS target"
    );
}

/// #793: a Java cross-file call into a class defined in another local file must
/// resolve to the canonical symbol so `callers`/`blast-radius` include the
/// caller. Java classes are file-granular (public-class convention), so the
/// import `app.svc.Service` maps to `.../svc/Service.java`. Exercises both
/// resolution shapes the task calls out — the static member call
/// `Service.start()` (resolves the method) and the constructor `new Service()`
/// (resolves the class) — across the index, rebuild, and sync-file paths.
#[test]
fn index_resolves_cross_file_local_java_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-file Java local-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("src/main/java/app/svc")).expect("create app/svc");
    fs::write(
        project.path().join("src/main/java/app/svc/Service.java"),
        "package app.svc;\n\nclass Service {\n    static void start() {}\n}\n",
    )
    .expect("write Service.java");
    fs::write(
        project.path().join("src/main/java/app/Main.java"),
        "package app;\n\nimport app.svc.Service;\n\nclass Main {\n    void run() {\n        Service.start();\n        new Service();\n    }\n}\n",
    )
    .expect("write Main.java");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": JAVA_LOCAL_PROJECT_ID,
            "name": "graph-standalone-java-local",
            "created_at": "2026-06-15T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = ProjectCleanup::new(&env.database_url, JAVA_LOCAL_PROJECT_ID);

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    let service_file = "src/main/java/app/svc/Service.java";
    let main_file = "src/main/java/app/Main.java";
    let start_id = required_symbol_id(&mut conn, JAVA_LOCAL_PROJECT_ID, service_file, "start");
    let service_id = required_symbol_id(&mut conn, JAVA_LOCAL_PROJECT_ID, service_file, "Service");

    // The static member call resolves to the method symbol and the constructor
    // call resolves to the class symbol, both in the imported class file.
    assert_eq!(
        resolved_call_target(&mut conn, JAVA_LOCAL_PROJECT_ID, main_file, "start"),
        Some(start_id.clone()),
        "Java static member call did not resolve to the canonical method"
    );
    assert_eq!(
        resolved_call_target(&mut conn, JAVA_LOCAL_PROJECT_ID, main_file, "Service"),
        Some(service_id.clone()),
        "Java constructor call did not resolve to the canonical class"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, JAVA_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    // Projection path 1+2: rebuild from resolved PostgreSQL facts, then both
    // cross-file callers must be reachable and each target must be a real
    // DEFINES-linked node (no phantom CALLS target).
    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "start", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "Service", "run", "after rebuild");

    let mut graph = phantom_graph_client(&env);
    assert_eq!(
        phantom_call_target_count(&mut graph, JAVA_LOCAL_PROJECT_ID),
        0,
        "no CALLS target may lack a DEFINES edge after rebuild"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, JAVA_LOCAL_PROJECT_ID, &start_id),
        "the resolved Java method target must be a defined, called CodeSymbol"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, JAVA_LOCAL_PROJECT_ID, &service_id),
        "the resolved Java class target must be a defined, called CodeSymbol"
    );

    let blast = json_command(&env, project.path(), &["blast-radius", "start"]);
    assert!(
        blast.get("center").is_some(),
        "blast-radius should report a center: {blast}"
    );

    // Projection path 3: sync-file must recreate the same canonical edges.
    let sync = run_gcode(
        &env,
        project.path(),
        &["graph", "sync-file", "--file", main_file],
    );
    assert_success(sync, "graph sync-file Main.java");
    assert_caller_present(&env, project.path(), "start", "run", "after sync-file");
    assert_eq!(
        phantom_call_target_count(&mut graph, JAVA_LOCAL_PROJECT_ID),
        0,
        "sync-file must not introduce a phantom CALLS target"
    );
}

mod common;

use common::{ProjectCleanup, cleanup_project};
use gobby_core::falkor::GraphClient;
use postgres::{Client, NoTls};
use serde_json::Value;
use std::collections::HashMap;
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

const LOCAL_IMPORT_PROJECT_ID: &str = "graph-standalone-local-import";
const NO_PHANTOM_PROJECT_ID: &str = "graph-standalone-no-phantom";
const GO_LOCAL_PROJECT_ID: &str = "graph-standalone-go-local";
const JAVA_LOCAL_PROJECT_ID: &str = "graph-standalone-java-local";
const CSHARP_LOCAL_PROJECT_ID: &str = "graph-standalone-csharp-local";
const KOTLIN_LOCAL_PROJECT_ID: &str = "graph-standalone-kotlin-local";
const RUBY_LOCAL_PROJECT_ID: &str = "graph-standalone-ruby-local";
const PHP_LOCAL_PROJECT_ID: &str = "graph-standalone-php-local";
const SWIFT_LOCAL_PROJECT_ID: &str = "graph-standalone-swift-local";
const DART_LOCAL_PROJECT_ID: &str = "graph-standalone-dart-local";
const ELIXIR_LOCAL_PROJECT_ID: &str = "graph-standalone-elixir-local";

/// End-to-end check for the post-write local-import resolution (#790): indexing
/// a two-file Python project must resolve the cross-file calls in `b.py` to the
/// real `code_symbols` ids defined in `a.py` (no recomputed UUID, so no phantom
/// node), leave no `local_import` rows behind, and surface the cross-file caller
/// through `callers`/`blast-radius` across all three projection paths
/// (index-time sync via rebuild, rebuild, and sync-file). Order-independence
/// holds for free: resolution reads the fully-populated `code_symbols` after the
/// whole run, regardless of which file was indexed first.
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

/// End-to-end proof that cross-file local C# call targets resolve to real symbol
/// ids and project as DEFINES-linked CALLS edges. Exercises both new resolution
/// shapes the task calls out — a namespace-imported simple-type member call
/// (`using App.Helpers;` then `Tool.Render()`) and a fully-qualified member call
/// (`App.Widgets.Widget.Build()`) — across the index, rebuild, and sync-file
/// paths.
#[test]
fn index_resolves_cross_file_local_csharp_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-file C# local-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("src/Helpers")).expect("create src/Helpers");
    fs::create_dir_all(project.path().join("src/Widgets")).expect("create src/Widgets");
    fs::write(
        project.path().join("src/Helpers/Tool.cs"),
        "namespace App.Helpers;\n\nclass Tool {\n    public static void Render() {}\n}\n",
    )
    .expect("write Tool.cs");
    fs::write(
        project.path().join("src/Widgets/Widget.cs"),
        "namespace App.Widgets;\n\nclass Widget {\n    public static void Build() {}\n}\n",
    )
    .expect("write Widget.cs");
    fs::write(
        project.path().join("src/Main.cs"),
        "namespace App;\n\nusing App.Helpers;\n\nclass Main {\n    void Run() {\n        Tool.Render();\n        App.Widgets.Widget.Build();\n    }\n}\n",
    )
    .expect("write Main.cs");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": CSHARP_LOCAL_PROJECT_ID,
            "name": "graph-standalone-csharp-local",
            "created_at": "2026-06-15T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = ProjectCleanup::new(&env.database_url, CSHARP_LOCAL_PROJECT_ID);

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    let tool_file = "src/Helpers/Tool.cs";
    let widget_file = "src/Widgets/Widget.cs";
    let main_file = "src/Main.cs";
    let render_id = required_symbol_id(&mut conn, CSHARP_LOCAL_PROJECT_ID, tool_file, "Render");
    let build_id = required_symbol_id(&mut conn, CSHARP_LOCAL_PROJECT_ID, widget_file, "Build");

    // The namespace-imported simple-type call and the fully-qualified call each
    // resolve to the canonical method symbol in the declaring type's file.
    assert_eq!(
        resolved_call_target(&mut conn, CSHARP_LOCAL_PROJECT_ID, main_file, "Render"),
        Some(render_id.clone()),
        "C# namespace-imported member call did not resolve to the canonical method"
    );
    assert_eq!(
        resolved_call_target(&mut conn, CSHARP_LOCAL_PROJECT_ID, main_file, "Build"),
        Some(build_id.clone()),
        "C# fully-qualified member call did not resolve to the canonical method"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, CSHARP_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    // Projection path 1+2: rebuild from resolved PostgreSQL facts, then both
    // cross-file callers must be reachable and each target must be a real
    // DEFINES-linked node (no phantom CALLS target).
    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "Render", "Run", "after rebuild");
    assert_caller_present(&env, project.path(), "Build", "Run", "after rebuild");

    let mut graph = phantom_graph_client(&env);
    assert_eq!(
        phantom_call_target_count(&mut graph, CSHARP_LOCAL_PROJECT_ID),
        0,
        "no CALLS target may lack a DEFINES edge after rebuild"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, CSHARP_LOCAL_PROJECT_ID, &render_id),
        "the resolved C# namespace-imported method target must be a defined, called CodeSymbol"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, CSHARP_LOCAL_PROJECT_ID, &build_id),
        "the resolved C# fully-qualified method target must be a defined, called CodeSymbol"
    );

    let blast = json_command(&env, project.path(), &["blast-radius", "Render"]);
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
    assert_success(sync, "graph sync-file Main.cs");
    assert_caller_present(&env, project.path(), "Render", "Run", "after sync-file");
    assert_eq!(
        phantom_call_target_count(&mut graph, CSHARP_LOCAL_PROJECT_ID),
        0,
        "sync-file must not introduce a phantom CALLS target"
    );
}

/// End-to-end proof that cross-file local Kotlin call targets resolve to real
/// symbol ids and project as DEFINES-linked CALLS edges. Exercises both shapes
/// the task calls out — a bare top-level function call from a package import
/// (`import app.helpers.render` then `render()`) and a class member call from a
/// type import (`import app.widgets.Widget` then `Widget.build()`) — across the
/// index, rebuild, and sync-file paths.
#[test]
fn index_resolves_cross_file_local_kotlin_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-file Kotlin local-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("src/helpers")).expect("create src/helpers");
    fs::create_dir_all(project.path().join("src/widgets")).expect("create src/widgets");
    fs::write(
        project.path().join("src/helpers/Helpers.kt"),
        "package app.helpers\n\nfun render() {}\n",
    )
    .expect("write Helpers.kt");
    fs::write(
        project.path().join("src/widgets/Widget.kt"),
        "package app.widgets\n\nclass Widget {\n    companion object {\n        fun build() {}\n    }\n}\n",
    )
    .expect("write Widget.kt");
    fs::write(
        project.path().join("src/Main.kt"),
        "package app\n\nimport app.helpers.render\nimport app.widgets.Widget\n\nfun run() {\n    render()\n    Widget.build()\n}\n",
    )
    .expect("write Main.kt");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": KOTLIN_LOCAL_PROJECT_ID,
            "name": "graph-standalone-kotlin-local",
            "created_at": "2026-06-15T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = ProjectCleanup::new(&env.database_url, KOTLIN_LOCAL_PROJECT_ID);

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    let helpers_file = "src/helpers/Helpers.kt";
    let widget_file = "src/widgets/Widget.kt";
    let main_file = "src/Main.kt";
    let render_id = required_symbol_id(&mut conn, KOTLIN_LOCAL_PROJECT_ID, helpers_file, "render");
    let build_id = required_symbol_id(&mut conn, KOTLIN_LOCAL_PROJECT_ID, widget_file, "build");

    // The bare top-level function call and the class member call each resolve to
    // the canonical symbol in the declaring package's file.
    assert_eq!(
        resolved_call_target(&mut conn, KOTLIN_LOCAL_PROJECT_ID, main_file, "render"),
        Some(render_id.clone()),
        "Kotlin package-imported top-level function call did not resolve to the canonical function"
    );
    assert_eq!(
        resolved_call_target(&mut conn, KOTLIN_LOCAL_PROJECT_ID, main_file, "build"),
        Some(build_id.clone()),
        "Kotlin type-imported member call did not resolve to the canonical method"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, KOTLIN_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    // Projection path 1+2: rebuild from resolved PostgreSQL facts, then both
    // cross-file callers must be reachable and each target must be a real
    // DEFINES-linked node (no phantom CALLS target).
    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "render", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "build", "run", "after rebuild");

    let mut graph = phantom_graph_client(&env);
    assert_eq!(
        phantom_call_target_count(&mut graph, KOTLIN_LOCAL_PROJECT_ID),
        0,
        "no CALLS target may lack a DEFINES edge after rebuild"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, KOTLIN_LOCAL_PROJECT_ID, &render_id),
        "the resolved Kotlin top-level function target must be a defined, called CodeSymbol"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, KOTLIN_LOCAL_PROJECT_ID, &build_id),
        "the resolved Kotlin member-call method target must be a defined, called CodeSymbol"
    );

    let blast = json_command(&env, project.path(), &["blast-radius", "render"]);
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
    assert_success(sync, "graph sync-file Main.kt");
    assert_caller_present(&env, project.path(), "render", "run", "after sync-file");
    assert_eq!(
        phantom_call_target_count(&mut graph, KOTLIN_LOCAL_PROJECT_ID),
        0,
        "sync-file must not introduce a phantom CALLS target"
    );
}

/// #795: a Ruby cross-file call onto a constant declared in another local file
/// must resolve to the canonical symbol so `callers`/`blast-radius` include the
/// caller. Ruby `require` loads files without importing names, so resolution is
/// constant-driven: a member call `Widget.build` resolves against the files that
/// declare `class Widget`, and the constructor `Widget.new` resolves to the
/// class itself. Both targets must become real `code_symbols` ids (no phantom),
/// across every projection path.
#[test]
fn index_resolves_cross_file_local_ruby_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-file Ruby local-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("lib")).expect("create lib");
    fs::write(
        project.path().join("lib/widget.rb"),
        "class Widget\n  def self.build(kind)\n  end\nend\n",
    )
    .expect("write widget.rb");
    fs::write(
        project.path().join("lib/runner.rb"),
        "require_relative \"widget\"\n\ndef run\n  Widget.build(\"box\")\n  Widget.new\nend\n",
    )
    .expect("write runner.rb");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": RUBY_LOCAL_PROJECT_ID,
            "name": "graph-standalone-ruby-local",
            "created_at": "2026-06-15T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = ProjectCleanup::new(&env.database_url, RUBY_LOCAL_PROJECT_ID);

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    let widget_file = "lib/widget.rb";
    let runner_file = "lib/runner.rb";
    let build_id = required_symbol_id(&mut conn, RUBY_LOCAL_PROJECT_ID, widget_file, "build");
    let widget_id = required_symbol_id(&mut conn, RUBY_LOCAL_PROJECT_ID, widget_file, "Widget");

    // The class-method call resolves to the method symbol; the `.new`
    // constructor resolves to the class symbol (carried under the constant name).
    assert_eq!(
        resolved_call_target(&mut conn, RUBY_LOCAL_PROJECT_ID, runner_file, "build"),
        Some(build_id.clone()),
        "Ruby constant member call did not resolve to the canonical method"
    );
    assert_eq!(
        resolved_call_target(&mut conn, RUBY_LOCAL_PROJECT_ID, runner_file, "Widget"),
        Some(widget_id.clone()),
        "Ruby `.new` constructor call did not resolve to the canonical class"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, RUBY_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    // Projection path 1+2: rebuild from resolved PostgreSQL facts, then both
    // cross-file callers must be reachable and each target must be a real
    // DEFINES-linked node (no phantom CALLS target).
    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "build", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "Widget", "run", "after rebuild");

    let mut graph = phantom_graph_client(&env);
    assert_eq!(
        phantom_call_target_count(&mut graph, RUBY_LOCAL_PROJECT_ID),
        0,
        "no CALLS target may lack a DEFINES edge after rebuild"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, RUBY_LOCAL_PROJECT_ID, &build_id),
        "the resolved Ruby member-call method target must be a defined, called CodeSymbol"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, RUBY_LOCAL_PROJECT_ID, &widget_id),
        "the resolved Ruby constructor class target must be a defined, called CodeSymbol"
    );

    let blast = json_command(&env, project.path(), &["blast-radius", "build"]);
    assert!(
        blast.get("center").is_some(),
        "blast-radius should report a center: {blast}"
    );

    // Projection path 3: sync-file must recreate the same canonical edges.
    let sync = run_gcode(
        &env,
        project.path(),
        &["graph", "sync-file", "--file", runner_file],
    );
    assert_success(sync, "graph sync-file runner.rb");
    assert_caller_present(&env, project.path(), "build", "run", "after sync-file");
    assert_eq!(
        phantom_call_target_count(&mut graph, RUBY_LOCAL_PROJECT_ID),
        0,
        "sync-file must not introduce a phantom CALLS target"
    );
}

/// End-to-end proof that cross-file local PHP call targets resolve to real symbol
/// ids and project as DEFINES-linked CALLS edges. Exercises the three shapes the
/// task calls out — a `use`-imported static member call (`Widget::build()`), a
/// constructor (`new Widget()`, which resolves the class), and a fully-qualified
/// static call (`\App\Service::dispatch()`) — across the index, rebuild, and
/// sync-file paths. No target may be a phantom CALLS node.
#[test]
fn index_resolves_cross_file_local_php_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-file PHP local-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("src")).expect("create src");
    fs::write(
        project.path().join("src/Widget.php"),
        "<?php\nnamespace App;\n\nclass Widget {\n    public static function build($kind) {}\n}\n",
    )
    .expect("write Widget.php");
    fs::write(
        project.path().join("src/Service.php"),
        "<?php\nnamespace App;\n\nclass Service {\n    public static function dispatch() {}\n}\n",
    )
    .expect("write Service.php");
    fs::write(
        project.path().join("src/main.php"),
        "<?php\nnamespace App;\n\nuse App\\Widget;\n\nfunction run() {\n    Widget::build(\"box\");\n    new Widget();\n    \\App\\Service::dispatch();\n}\n",
    )
    .expect("write main.php");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": PHP_LOCAL_PROJECT_ID,
            "name": "graph-standalone-php-local",
            "created_at": "2026-06-15T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = ProjectCleanup::new(&env.database_url, PHP_LOCAL_PROJECT_ID);

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    let widget_file = "src/Widget.php";
    let service_file = "src/Service.php";
    let main_file = "src/main.php";
    let build_id = required_symbol_id(&mut conn, PHP_LOCAL_PROJECT_ID, widget_file, "build");
    let widget_id = required_symbol_id(&mut conn, PHP_LOCAL_PROJECT_ID, widget_file, "Widget");
    let dispatch_id = required_symbol_id(&mut conn, PHP_LOCAL_PROJECT_ID, service_file, "dispatch");

    // The `use`-imported static call resolves to the method symbol, the
    // constructor resolves to the class symbol, and the fully-qualified static
    // call resolves to the method in its namespaced file.
    assert_eq!(
        resolved_call_target(&mut conn, PHP_LOCAL_PROJECT_ID, main_file, "build"),
        Some(build_id.clone()),
        "PHP static member call did not resolve to the canonical method"
    );
    assert_eq!(
        resolved_call_target(&mut conn, PHP_LOCAL_PROJECT_ID, main_file, "Widget"),
        Some(widget_id.clone()),
        "PHP constructor call did not resolve to the canonical class"
    );
    assert_eq!(
        resolved_call_target(&mut conn, PHP_LOCAL_PROJECT_ID, main_file, "dispatch"),
        Some(dispatch_id.clone()),
        "PHP fully-qualified static call did not resolve to the canonical method"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, PHP_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    // Projection path 1+2: rebuild from resolved PostgreSQL facts, then every
    // cross-file caller must be reachable and each target must be a real
    // DEFINES-linked node (no phantom CALLS target).
    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "build", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "Widget", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "dispatch", "run", "after rebuild");

    let mut graph = phantom_graph_client(&env);
    assert_eq!(
        phantom_call_target_count(&mut graph, PHP_LOCAL_PROJECT_ID),
        0,
        "no CALLS target may lack a DEFINES edge after rebuild"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, PHP_LOCAL_PROJECT_ID, &build_id),
        "the resolved PHP method target must be a defined, called CodeSymbol"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, PHP_LOCAL_PROJECT_ID, &widget_id),
        "the resolved PHP class target must be a defined, called CodeSymbol"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, PHP_LOCAL_PROJECT_ID, &dispatch_id),
        "the resolved PHP fully-qualified method target must be a defined, called CodeSymbol"
    );

    let blast = json_command(&env, project.path(), &["blast-radius", "build"]);
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
    assert_success(sync, "graph sync-file main.php");
    assert_caller_present(&env, project.path(), "build", "run", "after sync-file");
    assert_eq!(
        phantom_call_target_count(&mut graph, PHP_LOCAL_PROJECT_ID),
        0,
        "sync-file must not introduce a phantom CALLS target"
    );
}

/// End-to-end check for cross-file local call resolution in Swift (#798). Swift
/// has whole-module scope — files in a module call each other with no `import` —
/// so resolution narrows a bare callee name against every file sharing the
/// caller's module and is arbitrated by the post-write DB pass (#790). This
/// exercises both shapes the task calls out: a free-function call (`greet()`)
/// and a type initializer (`Widget()`, whose target struct is kind `type`),
/// across the index, rebuild, and sync-file paths. No target may be a phantom
/// CALLS node.
#[test]
fn index_resolves_cross_file_local_swift_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-file Swift local-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("Sources/App")).expect("create Sources/App");
    fs::write(
        project.path().join("Sources/App/Greeter.swift"),
        "func greet() -> String {\n    return \"hi\"\n}\n",
    )
    .expect("write Greeter.swift");
    fs::write(
        project.path().join("Sources/App/Widget.swift"),
        "struct Widget {}\n",
    )
    .expect("write Widget.swift");
    fs::write(
        project.path().join("Sources/App/main.swift"),
        "func run() {\n    _ = greet()\n    _ = Widget()\n}\n",
    )
    .expect("write main.swift");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": SWIFT_LOCAL_PROJECT_ID,
            "name": "graph-standalone-swift-local",
            "created_at": "2026-06-15T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = ProjectCleanup::new(&env.database_url, SWIFT_LOCAL_PROJECT_ID);

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    let greeter_file = "Sources/App/Greeter.swift";
    let widget_file = "Sources/App/Widget.swift";
    let main_file = "Sources/App/main.swift";
    let greet_id = required_symbol_id(&mut conn, SWIFT_LOCAL_PROJECT_ID, greeter_file, "greet");
    let widget_id = required_symbol_id(&mut conn, SWIFT_LOCAL_PROJECT_ID, widget_file, "Widget");

    // The free-function call resolves to the function symbol in its sibling
    // module file, and the initializer call resolves to the struct (kind `type`)
    // — both with no `import`, on whole-module scope alone.
    assert_eq!(
        resolved_call_target(&mut conn, SWIFT_LOCAL_PROJECT_ID, main_file, "greet"),
        Some(greet_id.clone()),
        "Swift free-function call did not resolve to the canonical function"
    );
    assert_eq!(
        resolved_call_target(&mut conn, SWIFT_LOCAL_PROJECT_ID, main_file, "Widget"),
        Some(widget_id.clone()),
        "Swift type initializer did not resolve to the canonical struct"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, SWIFT_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    // Projection path 1+2: rebuild from resolved PostgreSQL facts, then every
    // cross-file caller must be reachable and each target must be a real
    // DEFINES-linked node (no phantom CALLS target).
    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "greet", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "Widget", "run", "after rebuild");

    let mut graph = phantom_graph_client(&env);
    assert_eq!(
        phantom_call_target_count(&mut graph, SWIFT_LOCAL_PROJECT_ID),
        0,
        "no CALLS target may lack a DEFINES edge after rebuild"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, SWIFT_LOCAL_PROJECT_ID, &greet_id),
        "the resolved Swift function target must be a defined, called CodeSymbol"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, SWIFT_LOCAL_PROJECT_ID, &widget_id),
        "the resolved Swift struct target must be a defined, called CodeSymbol"
    );

    let blast = json_command(&env, project.path(), &["blast-radius", "greet"]);
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
    assert_success(sync, "graph sync-file main.swift");
    assert_caller_present(&env, project.path(), "greet", "run", "after sync-file");
    assert_eq!(
        phantom_call_target_count(&mut graph, SWIFT_LOCAL_PROJECT_ID),
        0,
        "sync-file must not introduce a phantom CALLS target"
    );
}

/// Resolves cross-file local Dart calls — a free function (`greet()`) reached
/// through an unaliased self-package import (`package:app/greeter.dart`), and a
/// constructor (`Widget()`) reached through an unaliased relative import
/// (`widget.dart`) — across the index, rebuild, and sync-file paths. Dart
/// imports name a whole file, so each bare call narrows the importing file's
/// local-import candidate set to the real symbol. No target may be a phantom
/// CALLS node.
#[test]
fn index_resolves_cross_file_local_dart_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-file Dart local-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("lib")).expect("create lib");
    fs::write(project.path().join("pubspec.yaml"), "name: app\n").expect("write pubspec.yaml");
    fs::write(
        project.path().join("lib/greeter.dart"),
        "String greet() {\n  return 'hi';\n}\n",
    )
    .expect("write greeter.dart");
    fs::write(project.path().join("lib/widget.dart"), "class Widget {}\n").expect("write widget");
    fs::write(
        project.path().join("lib/main.dart"),
        "import 'package:app/greeter.dart';\nimport 'widget.dart';\n\nvoid run() {\n  greet();\n  Widget();\n}\n",
    )
    .expect("write main.dart");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": DART_LOCAL_PROJECT_ID,
            "name": "graph-standalone-dart-local",
            "created_at": "2026-06-15T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = ProjectCleanup::new(&env.database_url, DART_LOCAL_PROJECT_ID);

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    let greeter_file = "lib/greeter.dart";
    let widget_file = "lib/widget.dart";
    let main_file = "lib/main.dart";
    let greet_id = required_symbol_id(&mut conn, DART_LOCAL_PROJECT_ID, greeter_file, "greet");
    let widget_id = required_symbol_id(&mut conn, DART_LOCAL_PROJECT_ID, widget_file, "Widget");

    // The free-function call resolves to the function in the imported
    // self-package file, and the constructor call resolves to the class in the
    // relatively-imported file — both cross-file, narrowed from the importing
    // file's local-import candidate set.
    assert_eq!(
        resolved_call_target(&mut conn, DART_LOCAL_PROJECT_ID, main_file, "greet"),
        Some(greet_id.clone()),
        "Dart free-function call did not resolve to the canonical function"
    );
    assert_eq!(
        resolved_call_target(&mut conn, DART_LOCAL_PROJECT_ID, main_file, "Widget"),
        Some(widget_id.clone()),
        "Dart constructor call did not resolve to the canonical class"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, DART_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    // Projection path 1+2: rebuild from resolved PostgreSQL facts, then every
    // cross-file caller must be reachable and each target a real DEFINES-linked
    // node (no phantom CALLS target).
    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "greet", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "Widget", "run", "after rebuild");

    let mut graph = phantom_graph_client(&env);
    assert_eq!(
        phantom_call_target_count(&mut graph, DART_LOCAL_PROJECT_ID),
        0,
        "no CALLS target may lack a DEFINES edge after rebuild"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, DART_LOCAL_PROJECT_ID, &greet_id),
        "the resolved Dart function target must be a defined, called CodeSymbol"
    );
    assert!(
        resolved_target_is_defined_and_called(&mut graph, DART_LOCAL_PROJECT_ID, &widget_id),
        "the resolved Dart class target must be a defined, called CodeSymbol"
    );

    let blast = json_command(&env, project.path(), &["blast-radius", "greet"]);
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
    assert_success(sync, "graph sync-file main.dart");
    assert_caller_present(&env, project.path(), "greet", "run", "after sync-file");
    assert_eq!(
        phantom_call_target_count(&mut graph, DART_LOCAL_PROJECT_ID),
        0,
        "sync-file must not introduce a phantom CALLS target"
    );
}

/// Resolves cross-file local Elixir calls across the three module-reference
/// shapes: an aliased call (`alias App.Greeter` → `Greeter.greet()`), a bare
/// imported call (`import App.MathX` → `tally()`), and a fully-qualified call
/// (`App.Format.shout()`). Each names a locally-declared module whose declaring
/// file is the candidate set; the post-write pass narrows it to the real
/// `code_symbols` id. No target may be a phantom CALLS node.
#[test]
fn index_resolves_cross_file_local_elixir_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-file Elixir local-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("lib")).expect("create lib");
    // Paren-less zero-arity `def`s so each function is indexed as a `method`
    // symbol that the post-write pass can resolve against.
    fs::write(
        project.path().join("lib/greeter.ex"),
        "defmodule App.Greeter do\n  def greet do\n    :hi\n  end\nend\n",
    )
    .expect("write greeter.ex");
    fs::write(
        project.path().join("lib/mathx.ex"),
        "defmodule App.MathX do\n  def tally do\n    0\n  end\nend\n",
    )
    .expect("write mathx.ex");
    fs::write(
        project.path().join("lib/format.ex"),
        "defmodule App.Format do\n  def shout do\n    :ok\n  end\nend\n",
    )
    .expect("write format.ex");
    fs::write(
        project.path().join("lib/sample.ex"),
        "defmodule App.Sample do\n  alias App.Greeter\n  import App.MathX\n\n  def run do\n    Greeter.greet()\n    tally()\n    App.Format.shout()\n  end\nend\n",
    )
    .expect("write sample.ex");
    fs::write(
        project.path().join(".gobby/gcode.json"),
        serde_json::json!({
            "id": ELIXIR_LOCAL_PROJECT_ID,
            "name": "graph-standalone-elixir-local",
            "created_at": "2026-06-15T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = ProjectCleanup::new(&env.database_url, ELIXIR_LOCAL_PROJECT_ID);

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    let greeter_file = "lib/greeter.ex";
    let mathx_file = "lib/mathx.ex";
    let format_file = "lib/format.ex";
    let sample_file = "lib/sample.ex";
    let greet_id = required_symbol_id(&mut conn, ELIXIR_LOCAL_PROJECT_ID, greeter_file, "greet");
    let tally_id = required_symbol_id(&mut conn, ELIXIR_LOCAL_PROJECT_ID, mathx_file, "tally");
    let shout_id = required_symbol_id(&mut conn, ELIXIR_LOCAL_PROJECT_ID, format_file, "shout");

    // Aliased, bare-imported, and fully-qualified calls each resolve to the
    // canonical function in the referenced local module — all cross-file.
    assert_eq!(
        resolved_call_target(&mut conn, ELIXIR_LOCAL_PROJECT_ID, sample_file, "greet"),
        Some(greet_id.clone()),
        "aliased Elixir call did not resolve to the canonical function"
    );
    assert_eq!(
        resolved_call_target(&mut conn, ELIXIR_LOCAL_PROJECT_ID, sample_file, "tally"),
        Some(tally_id.clone()),
        "bare imported Elixir call did not resolve to the canonical function"
    );
    assert_eq!(
        resolved_call_target(&mut conn, ELIXIR_LOCAL_PROJECT_ID, sample_file, "shout"),
        Some(shout_id.clone()),
        "fully-qualified Elixir call did not resolve to the canonical function"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, ELIXIR_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    // Projection path 1+2: rebuild from resolved PostgreSQL facts, then every
    // cross-file caller must be reachable and each target a real DEFINES-linked
    // node (no phantom CALLS target).
    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "greet", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "tally", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "shout", "run", "after rebuild");

    let mut graph = phantom_graph_client(&env);
    assert_eq!(
        phantom_call_target_count(&mut graph, ELIXIR_LOCAL_PROJECT_ID),
        0,
        "no CALLS target may lack a DEFINES edge after rebuild"
    );
    for id in [&greet_id, &tally_id, &shout_id] {
        assert!(
            resolved_target_is_defined_and_called(&mut graph, ELIXIR_LOCAL_PROJECT_ID, id),
            "the resolved Elixir function target must be a defined, called CodeSymbol"
        );
    }

    let blast = json_command(&env, project.path(), &["blast-radius", "greet"]);
    assert!(
        blast.get("center").is_some(),
        "blast-radius should report a center: {blast}"
    );

    // Projection path 3: sync-file must recreate the same canonical edges.
    let sync = run_gcode(
        &env,
        project.path(),
        &["graph", "sync-file", "--file", sample_file],
    );
    assert_success(sync, "graph sync-file sample.ex");
    assert_caller_present(&env, project.path(), "greet", "run", "after sync-file");
    assert_eq!(
        phantom_call_target_count(&mut graph, ELIXIR_LOCAL_PROJECT_ID),
        0,
        "sync-file must not introduce a phantom CALLS target"
    );
}

/// Regression for #791 (no phantom / cross-file call targets). The #790 rework
/// resolves local-import calls against the real `code_symbols` ids instead of a
/// recomputed UUID, so a `CALLS` edge can never land on a fabricated (phantom)
/// `CodeSymbol`. This locks that invariant across Python, Rust, and JS/TS using
/// decorated / attributed / doc-commented and aliased top-level definitions —
/// the exact shapes whose byte offsets shifted under the retired file-scan
/// pre-scan and could have desynced a recomputed id. After indexing and
/// projecting the graph, every `CALLS`-target `CodeSymbol` must be backed by a
/// `DEFINES` edge (no orphan/phantom nodes), and `callers` must surface each
/// cross-file caller.
///
/// #790 deleted the per-language pre-scan and the parse-time UUID compute
/// entirely, so the original criteria's "pre-scan byte_start/kind parity" check
/// and `with_symbol_target` UUID phantom-guard are now vacuous. The
/// services-free half — that decorated/attributed definitions still record a
/// resolvable `local_import` — is covered by the `decorated_*` parser unit
/// tests; this is the live-graph half.
#[test]
fn index_creates_no_phantom_call_targets_across_languages() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping no-phantom cross-file resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    let root = project.path();
    fs::create_dir_all(root.join(".gobby")).expect("create .gobby");
    fs::create_dir_all(root.join("src")).expect("create src");

    fs::write(
        root.join(".gobby/gcode.json"),
        serde_json::json!({
            "id": NO_PHANTOM_PROJECT_ID,
            "name": "graph-standalone-no-phantom",
            "created_at": "2026-06-15T00:00:00Z"
        })
        .to_string(),
    )
    .expect("write gcode identity");
    // Crate + package metadata so Rust self-crate and JS self-package
    // resolution have a name to anchor on.
    fs::write(root.join("Cargo.toml"), "[package]\nname = \"app\"\n").expect("write Cargo.toml");
    fs::write(
        root.join("package.json"),
        r#"{"name":"app","dependencies":{}}"#,
    )
    .expect("write package.json");

    // Python: a decorated function and a documented class, imported (one
    // aliased) by a sibling module.
    fs::write(
        root.join("defs.py"),
        "def _mark(fn):\n    return fn\n\n\n@_mark\ndef decorated_target():\n    \"\"\"Doc-commented, decorated target.\"\"\"\n    return 1\n\n\nclass DecoratedService:\n    \"\"\"Doc-commented service.\"\"\"\n\n    def __init__(self):\n        pass\n",
    )
    .expect("write defs.py");
    fs::write(
        root.join("caller.py"),
        "from defs import decorated_target\nfrom defs import DecoratedService as Svc\n\n\ndef py_caller():\n    decorated_target()\n    Svc()\n",
    )
    .expect("write caller.py");

    // Rust: an attributed + doc-commented target in a sibling module, called via
    // a `crate::` use binding.
    fs::write(
        root.join("src/service.rs"),
        "/// Doc-commented, attributed cross-file target.\n#[inline]\npub fn rust_target() {}\n",
    )
    .expect("write src/service.rs");
    fs::write(
        root.join("src/lib.rs"),
        "pub mod service;\n\nuse crate::service::rust_target;\n\n/// Doc-commented caller.\npub fn rust_caller() {\n    rust_target();\n}\n",
    )
    .expect("write src/lib.rs");

    // JavaScript: a JSDoc-commented export imported under an alias.
    fs::write(
        root.join("src/js_target.js"),
        "/** Doc-commented target. */\nexport function jsTarget() {}\n",
    )
    .expect("write src/js_target.js");
    fs::write(
        root.join("src/js_caller.js"),
        "import { jsTarget as runJsTarget } from \"./js_target\";\n\nexport function jsCaller() {\n  runJsTarget();\n}\n",
    )
    .expect("write src/js_caller.js");

    // TypeScript: a JSDoc-commented export imported by name.
    fs::write(
        root.join("src/ts_target.ts"),
        "/** Doc-commented target. */\nexport function tsTarget(): void {}\n",
    )
    .expect("write src/ts_target.ts");
    fs::write(
        root.join("src/ts_caller.ts"),
        "import { tsTarget } from \"./ts_target\";\n\nexport function tsCaller(): void {\n  tsTarget();\n}\n",
    )
    .expect("write src/ts_caller.ts");

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = ProjectCleanup::new(&env.database_url, NO_PHANTOM_PROJECT_ID);

    assert_success(
        run_gcode(&env, root, &["index", "--full"]),
        "gcode index --full",
    );

    // (definition file, definition name, caller file, recorded callee name after
    // the alias resolves back to the original export/symbol name).
    let cases = [
        (
            "defs.py",
            "decorated_target",
            "caller.py",
            "decorated_target",
        ),
        (
            "defs.py",
            "DecoratedService",
            "caller.py",
            "DecoratedService",
        ),
        ("src/service.rs", "rust_target", "src/lib.rs", "rust_target"),
        (
            "src/js_target.js",
            "jsTarget",
            "src/js_caller.js",
            "jsTarget",
        ),
        (
            "src/ts_target.ts",
            "tsTarget",
            "src/ts_caller.ts",
            "tsTarget",
        ),
    ];

    // Every cross-file local call resolved to the real indexed id, and no
    // pending `local_import` row survived the run.
    let mut target_ids = Vec::new();
    for (def_file, def_name, caller_file, callee_name) in cases {
        let target_id = required_symbol_id(&mut conn, NO_PHANTOM_PROJECT_ID, def_file, def_name);
        assert_eq!(
            resolved_call_target(&mut conn, NO_PHANTOM_PROJECT_ID, caller_file, callee_name),
            Some(target_id.clone()),
            "{caller_file} call `{callee_name}` did not resolve to {def_file}:{def_name}"
        );
        target_ids.push((def_name, target_id));
    }
    assert_eq!(
        pending_local_import_count(&mut conn, NO_PHANTOM_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    // Project the graph and assert the no-phantom invariant directly in
    // FalkorDB: no `CALLS`-target `CodeSymbol` lacks a `DEFINES` edge, and every
    // resolved target id is a defined, called node.
    let rebuilt = json_command(&env, root, &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);

    let mut graph = phantom_graph_client(&env);
    assert_eq!(
        phantom_call_target_count(&mut graph, NO_PHANTOM_PROJECT_ID),
        0,
        "a CALLS edge targeted a CodeSymbol with no DEFINES edge (phantom) after rebuild"
    );
    for (def_name, target_id) in &target_ids {
        assert!(
            resolved_target_is_defined_and_called(&mut graph, NO_PHANTOM_PROJECT_ID, target_id),
            "resolved target `{def_name}` ({target_id}) is not a defined, called CodeSymbol"
        );
    }

    // Each language's cross-file caller is reachable through `callers`.
    for (target, caller) in [
        ("decorated_target", "py_caller"),
        ("rust_target", "rust_caller"),
        ("jsTarget", "jsCaller"),
        ("tsTarget", "tsCaller"),
    ] {
        assert_caller_present(&env, root, target, caller, "after rebuild");
    }

    // The sync-file projection path must also avoid phantoms.
    assert_success(
        run_gcode(&env, root, &["graph", "sync-file", "--file", "caller.py"]),
        "graph sync-file caller.py",
    );
    let mut graph = phantom_graph_client(&env);
    assert_eq!(
        phantom_call_target_count(&mut graph, NO_PHANTOM_PROJECT_ID),
        0,
        "a CALLS edge targeted a phantom CodeSymbol after sync-file"
    );
    assert_caller_present(
        &env,
        root,
        "decorated_target",
        "py_caller",
        "after sync-file",
    );
}

/// Connect a read client to the shared code graph (project-scoped by node
/// property), reusing the standalone FalkorDB env.
fn phantom_graph_client(env: &StandaloneEnv) -> GraphClient {
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
fn phantom_call_target_count(graph: &mut GraphClient, project_id: &str) -> i64 {
    let params = HashMap::from([("project".to_string(), project_id.to_string())]);
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
fn resolved_target_is_defined_and_called(
    graph: &mut GraphClient,
    project_id: &str,
    symbol_id: &str,
) -> bool {
    let params = HashMap::from([
        ("project".to_string(), project_id.to_string()),
        ("id".to_string(), symbol_id.to_string()),
    ]);
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

fn assert_caller_present(
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

fn required_symbol_id(conn: &mut Client, project_id: &str, file_path: &str, name: &str) -> String {
    conn.query_one(
        "SELECT id FROM code_symbols WHERE project_id = $1 AND file_path = $2 AND name = $3",
        &[&project_id, &file_path, &name],
    )
    .unwrap_or_else(|err| panic!("symbol {file_path}:{name} not indexed: {err}"))
    .get::<_, String>(0)
}

fn resolved_call_target(
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

fn pending_local_import_count(conn: &mut Client, project_id: &str) -> i64 {
    conn.query_one(
        "SELECT COUNT(*) FROM code_calls
         WHERE project_id = $1 AND callee_target_kind = 'local_import'",
        &[&project_id],
    )
    .expect("count local_import rows")
    .get::<_, i64>(0)
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

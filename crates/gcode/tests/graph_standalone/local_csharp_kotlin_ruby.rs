use super::support::*;

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
    assert_blast_radius_reports_affected_callers(&blast);

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
    assert_blast_radius_reports_affected_callers(&blast);

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
    assert_blast_radius_reports_affected_callers(&blast);

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

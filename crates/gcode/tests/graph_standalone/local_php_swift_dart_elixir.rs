use super::support::*;

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
    assert_blast_radius_reports_affected_callers(&blast);

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
    assert_blast_radius_reports_affected_callers(&blast);

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
    assert_blast_radius_reports_affected_callers(&blast);

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
    fs::write(
        project.path().join("lib/greeter.ex"),
        "defmodule App.Greeter do\n  def greet(name) do\n    {:hi, name}\n  end\nend\n",
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
        "defmodule App.Sample do\n  alias App.Greeter\n  import App.MathX\n\n  def run do\n    Greeter.greet(\"Ada\")\n    tally()\n    App.Format.shout()\n  end\nend\n",
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
    assert_blast_radius_reports_affected_callers(&blast);

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

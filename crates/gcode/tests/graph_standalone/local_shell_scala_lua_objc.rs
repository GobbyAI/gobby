use super::support::*;
use postgres::{Client, NoTls};
use std::fs;

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

fn clean_project(
    env: &StandaloneEnv,
    conn: &mut Client,
    project_id: &'static str,
    label: &str,
) -> ProjectCleanup {
    cleanup_project(conn, project_id)
        .unwrap_or_else(|err| panic!("cleanup {label} project: {err}"));
    ProjectCleanup::new(&env.database_url, project_id)
}

fn resolved_call_targets(
    conn: &mut Client,
    project_id: &str,
    file_path: &str,
    callee_name: &str,
) -> Vec<String> {
    conn.query(
        "SELECT callee_symbol_id, callee_target_kind FROM code_calls
         WHERE project_id = $1 AND file_path = $2 AND callee_name = $3
         ORDER BY line, callee_symbol_id",
        &[&project_id, &file_path, &callee_name],
    )
    .expect("read code_calls rows")
    .into_iter()
    .filter_map(|row| {
        let kind: String = row.get("callee_target_kind");
        let symbol_id: String = row.get("callee_symbol_id");
        (kind == "symbol" && !symbol_id.is_empty()).then_some(symbol_id)
    })
    .collect()
}

#[test]
fn index_resolves_cross_file_local_bash_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-file Bash local-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("scripts/lib")).expect("create scripts/lib");
    fs::write(
        project.path().join("scripts/lib/helpers.sh"),
        "greet() {\n  echo hi\n}\n",
    )
    .expect("write helpers.sh");
    fs::write(
        project.path().join("scripts/lib/more.bash"),
        "announce() {\n  echo hi\n}\n",
    )
    .expect("write more.bash");
    fs::write(
        project.path().join("scripts/main.sh"),
        // Bare `source lib/helpers.sh` remains intentionally unresolved: it can
        // be PATH-like shell lookup, so local resolution only accepts explicit
        // `./` or `../` file references.
        "source ./lib/helpers.sh\n. ./lib/more.bash\n\nrun() {\n  greet\n  announce\n}\n",
    )
    .expect("write main.sh");
    write_identity(
        project.path(),
        BASH_LOCAL_PROJECT_ID,
        "graph-standalone-bash-local",
    );

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = clean_project(&env, &mut conn, BASH_LOCAL_PROJECT_ID, "Bash");

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    let main_file = "scripts/main.sh";
    let greet_id = required_symbol_id(
        &mut conn,
        BASH_LOCAL_PROJECT_ID,
        "scripts/lib/helpers.sh",
        "greet",
    );
    let announce_id = required_symbol_id(
        &mut conn,
        BASH_LOCAL_PROJECT_ID,
        "scripts/lib/more.bash",
        "announce",
    );
    assert_eq!(
        resolved_call_target(&mut conn, BASH_LOCAL_PROJECT_ID, main_file, "greet"),
        Some(greet_id.clone()),
        "Bash sourced function call did not resolve to the canonical helper symbol"
    );
    assert_eq!(
        resolved_call_target(&mut conn, BASH_LOCAL_PROJECT_ID, main_file, "announce"),
        Some(announce_id.clone()),
        "Bash dot-sourced function call did not resolve to the canonical helper symbol"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, BASH_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "greet", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "announce", "run", "after rebuild");
}

#[test]
fn index_resolves_cross_file_local_scala_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-file Scala local-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("src/main/scala/app/helpers")).expect("create helpers");
    fs::create_dir_all(project.path().join("src/main/scala/app/widgets")).expect("create widgets");
    fs::write(
        project
            .path()
            .join("src/main/scala/app/helpers/Helpers.scala"),
        "package app.helpers\n\ndef render(): Unit = {}\n",
    )
    .expect("write Helpers.scala");
    fs::write(
        project.path().join("src/main/scala/app/widgets/Widget.scala"),
        "package app.widgets\n\nclass Widget {\n  def build(): Unit = {}\n}\ndef buildWidget(): Widget = new Widget()\n",
    )
    .expect("write Widget.scala");
    fs::write(
        project.path().join("src/main/scala/app/Main.scala"),
        "package app\n\nimport app.helpers.render\nimport app.widgets.{Widget, buildWidget}\n\nobject Main {\n  def run(): Unit = {\n    render()\n    Widget.build()\n    new Widget()\n    buildWidget()\n  }\n}\n",
    )
    .expect("write Main.scala");
    write_identity(
        project.path(),
        SCALA_LOCAL_PROJECT_ID,
        "graph-standalone-scala-local",
    );

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = clean_project(&env, &mut conn, SCALA_LOCAL_PROJECT_ID, "Scala");

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    let main_file = "src/main/scala/app/Main.scala";
    let widget_file = "src/main/scala/app/widgets/Widget.scala";
    let render_id = required_symbol_id(
        &mut conn,
        SCALA_LOCAL_PROJECT_ID,
        "src/main/scala/app/helpers/Helpers.scala",
        "render",
    );
    let build_id = required_symbol_id(&mut conn, SCALA_LOCAL_PROJECT_ID, widget_file, "build");
    let widget_id = required_symbol_id(&mut conn, SCALA_LOCAL_PROJECT_ID, widget_file, "Widget");
    let build_widget_id = required_symbol_id(
        &mut conn,
        SCALA_LOCAL_PROJECT_ID,
        widget_file,
        "buildWidget",
    );

    assert_eq!(
        resolved_call_target(&mut conn, SCALA_LOCAL_PROJECT_ID, main_file, "render"),
        Some(render_id.clone()),
        "Scala imported function call did not resolve to the canonical symbol"
    );
    assert_eq!(
        resolved_call_target(&mut conn, SCALA_LOCAL_PROJECT_ID, main_file, "build"),
        Some(build_id.clone()),
        "Scala imported class member call did not resolve to the canonical method"
    );
    assert_eq!(
        resolved_call_target(&mut conn, SCALA_LOCAL_PROJECT_ID, main_file, "Widget"),
        Some(widget_id.clone()),
        "Scala constructor call did not resolve to the canonical class"
    );
    assert_eq!(
        resolved_call_target(&mut conn, SCALA_LOCAL_PROJECT_ID, main_file, "buildWidget",),
        Some(build_widget_id.clone()),
        "Scala grouped import call did not resolve to the canonical symbol"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, SCALA_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "render", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "build", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "buildWidget", "run", "after rebuild");
}

#[test]
fn index_resolves_cross_file_local_lua_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-file Lua local-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("lua/app")).expect("create lua/app");
    fs::write(
        project.path().join("lua/app/widgets.lua"),
        "local M = {}\nfunction M.build() end\nfunction M:draw() end\nfunction M.make() end\nreturn M\n",
    )
    .expect("write widgets.lua");
    fs::write(
        project.path().join("lua/app/init.lua"),
        "local M = {}\nfunction M.start() end\nreturn M\n",
    )
    .expect("write init.lua");
    fs::write(
        project.path().join("lua/main.lua"),
        "local widgets = require(\"app.widgets\")\nlocal make = require(\"app.widgets\").make\nlocal app = require(\"app\")\n\nfunction run()\n  widgets.build()\n  widgets:draw()\n  make()\n  app.start()\n  require(\"app.widgets\").build()\nend\n",
    )
    .expect("write main.lua");
    write_identity(
        project.path(),
        LUA_LOCAL_PROJECT_ID,
        "graph-standalone-lua-local",
    );

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = clean_project(&env, &mut conn, LUA_LOCAL_PROJECT_ID, "Lua");

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    let main_file = "lua/main.lua";
    let widgets_file = "lua/app/widgets.lua";
    let build_id = required_symbol_id(&mut conn, LUA_LOCAL_PROJECT_ID, widgets_file, "build");
    let draw_id = required_symbol_id(&mut conn, LUA_LOCAL_PROJECT_ID, widgets_file, "draw");
    let make_id = required_symbol_id(&mut conn, LUA_LOCAL_PROJECT_ID, widgets_file, "make");
    let start_id = required_symbol_id(&mut conn, LUA_LOCAL_PROJECT_ID, "lua/app/init.lua", "start");

    assert_eq!(
        resolved_call_targets(&mut conn, LUA_LOCAL_PROJECT_ID, main_file, "build"),
        vec![build_id.clone(), build_id.clone()],
        "both Lua require member calls must resolve to the canonical symbol"
    );
    assert_eq!(
        resolved_call_target(&mut conn, LUA_LOCAL_PROJECT_ID, main_file, "draw"),
        Some(draw_id.clone()),
        "Lua require method call did not resolve to the canonical symbol"
    );
    assert_eq!(
        resolved_call_target(&mut conn, LUA_LOCAL_PROJECT_ID, main_file, "make"),
        Some(make_id.clone()),
        "Lua require member alias call did not resolve to the canonical symbol"
    );
    assert_eq!(
        resolved_call_target(&mut conn, LUA_LOCAL_PROJECT_ID, main_file, "start"),
        Some(start_id.clone()),
        "Lua package init member call did not resolve to the canonical symbol"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, LUA_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "build", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "draw", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "make", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "start", "run", "after rebuild");
}

#[test]
fn index_resolves_cross_file_local_objc_calls() {
    let Some(env) = StandaloneEnv::from_env() else {
        eprintln!(
            "skipping cross-file Objective-C local-call resolution; set GCODE_GRAPH_STANDALONE_DATABASE_URL, GCODE_GRAPH_STANDALONE_FALKOR_HOST, and GCODE_GRAPH_STANDALONE_FALKOR_PORT"
        );
        return;
    };

    let project = tempfile::tempdir().expect("temp project");
    fs::create_dir_all(project.path().join(".gobby")).expect("create .gobby");
    fs::create_dir_all(project.path().join("Sources/App")).expect("create Sources/App");
    fs::write(
        project.path().join("Sources/App/Widget.h"),
        "#import <Foundation/Foundation.h>\n\n@interface Widget : NSObject\n- (void)render;\n@end\n",
    )
    .expect("write Widget.h");
    fs::write(
        project.path().join("Sources/App/Helpers.h"),
        "@import Foundation;\n#import \"Widget.h\"\n\nvoid WidgetRender(Widget *widget);\n",
    )
    .expect("write Helpers.h");
    fs::write(
        project.path().join("Sources/App/Sample.m"),
        "#import \"Widget.h\"\n#import \"Helpers.h\"\n\nvoid run(void) {\n  Widget *widget = [Widget new];\n  [widget render];\n  WidgetRender(widget);\n}\n",
    )
    .expect("write Sample.m");
    write_identity(
        project.path(),
        OBJC_LOCAL_PROJECT_ID,
        "graph-standalone-objc-local",
    );

    let mut conn = Client::connect(&env.database_url, NoTls).expect("connect PostgreSQL");
    let _cleanup = clean_project(&env, &mut conn, OBJC_LOCAL_PROJECT_ID, "Objective-C");

    let indexed = run_gcode(&env, project.path(), &["index", "--full"]);
    assert_success(indexed, "gcode index --full");

    let main_file = "Sources/App/Sample.m";
    let render_id = required_symbol_id(
        &mut conn,
        OBJC_LOCAL_PROJECT_ID,
        "Sources/App/Widget.h",
        "render",
    );
    let render_fn_id = required_symbol_id(
        &mut conn,
        OBJC_LOCAL_PROJECT_ID,
        "Sources/App/Helpers.h",
        "WidgetRender",
    );

    // Multi-keyword messages such as `[obj doFoo:x bar:y]` currently capture
    // only `doFoo`, so this DB-resolution coverage stays on single-keyword
    // selectors until selector extraction preserves the full Objective-C name.
    assert_eq!(
        resolved_call_target(&mut conn, OBJC_LOCAL_PROJECT_ID, main_file, "render"),
        Some(render_id.clone()),
        "Objective-C imported header message did not resolve to the canonical method"
    );
    assert_eq!(
        resolved_call_target(&mut conn, OBJC_LOCAL_PROJECT_ID, main_file, "WidgetRender",),
        Some(render_fn_id.clone()),
        "Objective-C imported C function did not resolve to the canonical header symbol"
    );
    assert_eq!(
        pending_local_import_count(&mut conn, OBJC_LOCAL_PROJECT_ID),
        0,
        "no local_import rows should survive a completed index run"
    );

    let rebuilt = json_command(&env, project.path(), &["graph", "rebuild"]);
    assert_eq!(rebuilt["success"], true);
    assert_caller_present(&env, project.path(), "render", "run", "after rebuild");
    assert_caller_present(&env, project.path(), "WidgetRender", "run", "after rebuild");
}

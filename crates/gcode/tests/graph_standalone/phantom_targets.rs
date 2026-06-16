use super::support::*;

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

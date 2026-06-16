use super::common::{parse_bash, parse_source};

macro_rules! assert_bash_local_import {
    ($parsed:expr, $callee_name:expr, $expected_file:expr) => {{
        let callee_name: &str = $callee_name;
        let call = $parsed
            .calls
            .iter()
            .find(|call| {
                call.callee_target_kind.as_str() == "local_import"
                    && call.callee_name == callee_name
            })
            .unwrap_or_else(|| panic!("missing local_import call for {callee_name}"));
        let candidates = call.local_import_candidate_files();
        assert!(
            candidates.iter().any(|file| file == $expected_file),
            "candidate files {candidates:?} did not contain {}",
            $expected_file
        );
    }};
}

#[test]
fn extracts_bash_function_definitions_and_same_file_calls() {
    let parsed = parse_bash(
        r#"
build_app() {
  compile_assets
}

function compile_assets {
  echo "assets"
}
"#,
        &[],
    );

    let function_names = parsed
        .symbols
        .iter()
        .filter(|symbol| symbol.kind == "function")
        .map(|symbol| symbol.name.as_str())
        .collect::<Vec<_>>();
    assert!(function_names.contains(&"build_app"));
    assert!(function_names.contains(&"compile_assets"));

    let compile_id = parsed
        .symbols
        .iter()
        .find(|symbol| symbol.name == "compile_assets")
        .expect("compile_assets symbol")
        .id
        .clone();
    let call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "compile_assets")
        .expect("compile_assets call");
    assert_eq!(call.callee_target_kind.as_str(), "symbol");
    assert_eq!(call.callee_symbol_id, Some(compile_id));
}

#[test]
fn resolves_sourced_bash_files_as_local_import_candidates() {
    let parsed = parse_bash(
        r#"
source ./lib/helpers.sh
. ./lib/more.bash
source lib/bare.sh

run() {
  greet
  announce
  bare
}
"#,
        &[
            ("scripts/lib/helpers.sh", "greet() { echo hi; }\n"),
            ("scripts/lib/more.bash", "announce() { echo hi; }\n"),
            ("scripts/lib/bare.sh", "bare() { echo hi; }\n"),
        ],
    );

    let imports = parsed
        .imports
        .iter()
        .map(|import| import.module_name.as_str())
        .collect::<Vec<_>>();
    assert!(imports.contains(&"./lib/helpers.sh"));
    assert!(imports.contains(&"./lib/more.bash"));
    assert!(imports.contains(&"lib/bare.sh"));

    assert_bash_local_import!(&parsed, "greet", "scripts/lib/helpers.sh");
    assert_bash_local_import!(&parsed, "announce", "scripts/lib/more.bash");
    assert_bash_local_import!(&parsed, "bare", "scripts/lib/bare.sh");
    assert!(
        parsed
            .calls
            .iter()
            .all(|call| { call.callee_name != "source" && call.callee_name != "." })
    );
}

#[test]
fn ignores_dynamic_bash_source_targets_for_local_resolution() {
    let parsed = parse_bash(
        r#"
source "$SCRIPT_DIR/helpers.sh"

run() {
  greet
}
"#,
        &[("scripts/lib/helpers.sh", "greet() { echo hi; }\n")],
    );

    let call = parsed
        .calls
        .iter()
        .find(|call| call.callee_name == "greet")
        .expect("greet call");
    assert_eq!(call.callee_target_kind.as_str(), "unresolved");
}

#[test]
fn detects_bash_extension_files() {
    let parsed = parse_source(
        "scripts/setup.bash",
        r#"
prepare_env() {
  echo ready
}
"#,
        &[],
    );

    assert!(
        parsed
            .symbols
            .iter()
            .any(|symbol| symbol.name == "prepare_env" && symbol.kind == "function")
    );
}

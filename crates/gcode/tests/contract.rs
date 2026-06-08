use std::process::Command;

use serde_json::Value;

fn pinned_contract() -> Value {
    serde_json::from_str(include_str!("../contract/gcode.contract.json")).expect("pinned contract")
}

fn shared_graph_schema_doc() -> &'static str {
    include_str!("../../../docs/contracts/shared-graph-schema.md")
}

fn code_graph_writer() -> &'static str {
    include_str!("../src/graph/code_graph/write.rs")
}

#[test]
fn contract_builder_matches_pinned_json() {
    let actual = serde_json::to_value(gobby_code::contract::contract()).expect("contract json");
    assert_eq!(actual, pinned_contract());
}

#[test]
fn contract_command_emits_pinned_json() {
    let output = Command::new(env!("CARGO_BIN_EXE_gcode"))
        .args(["contract", "--format", "json"])
        .output()
        .expect("run gcode contract");

    assert!(
        output.status.success(),
        "gcode contract failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let actual: Value = serde_json::from_slice(&output.stdout).expect("contract stdout json");
    assert_eq!(actual, pinned_contract());
}

#[test]
fn code_graph_writer_matches_shared_schema_contract() {
    let docs = shared_graph_schema_doc();
    let writer = code_graph_writer();

    for token in [
        "CodeFile",
        "CodeModule",
        "CodeSymbol",
        "ExternalSymbol",
        "UnresolvedCallee",
        "IMPORTS",
        "DEFINES",
        "CALLS",
        "project",
    ] {
        assert!(docs.contains(token), "{token} missing from schema docs");
        assert!(writer.contains(token), "{token} missing from gcode writer");
    }
}

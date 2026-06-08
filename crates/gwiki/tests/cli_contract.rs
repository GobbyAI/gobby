mod common;

use serde_json::Value;

fn pinned_contract() -> Value {
    serde_json::from_str(include_str!("../contract/gwiki.contract.json")).expect("pinned contract")
}

#[test]
fn contract_builder_matches_pinned_json() {
    let actual = serde_json::to_value(gobby_wiki::contract::contract()).expect("contract json");
    assert_eq!(actual, pinned_contract());
}

#[test]
fn contract_command_emits_pinned_json() {
    let output = common::gwiki_command()
        .args(["contract", "--format", "json"])
        .output()
        .expect("run gwiki contract");

    assert!(
        output.status.success(),
        "gwiki contract failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let actual: Value = serde_json::from_slice(&output.stdout).expect("contract stdout json");
    assert_eq!(actual, pinned_contract());
}

#[test]
fn compile_contract_tracks_compile_json_payload_keys() {
    let contract = gobby_wiki::contract::contract();
    let compile = contract
        .commands
        .iter()
        .find(|command| command.name == "compile")
        .expect("compile command contract");

    let expected_keys = vec![
        "command",
        "scope",
        "status",
        "target_kind",
        "outline",
        "daemon_synthesis_available",
        "article_path",
        "source_paths",
        "index_path",
        "handoff_id",
        "page_writes",
        "prompt",
    ];

    assert_eq!(compile.json_output_keys, expected_keys);
}

#[test]
fn graph_context_contract_tracks_dependency_classification() {
    let contract = pinned_contract();
    let graph_context = contract["commands"]
        .as_array()
        .expect("commands array")
        .iter()
        .find(|command| command["name"] == "graph-context")
        .expect("graph-context command contract");

    assert_eq!(
        graph_context["hard_dependencies"],
        serde_json::json!(["PostgreSQL"])
    );
    assert_eq!(
        graph_context["optional_dependencies"],
        serde_json::json!(["FalkorDB", "shared code graph"])
    );
    assert_eq!(graph_context["multimodal"], "none");
    assert_eq!(
        graph_context["degradation"],
        serde_json::json!({
            "output_shape": "wiki-link-only neighborhood",
            "metadata_keys": [
                "warnings[]",
                "degradation.degraded",
                "degradation.degraded_sources[]"
            ]
        })
    );

    let expected_keys = [
        "command",
        "scope",
        "context",
        "source_bundle",
        "trust",
        "freshness",
        "audit",
        "warnings",
        "degradation",
    ];
    assert_eq!(
        graph_context["json_output_keys"],
        serde_json::json!(expected_keys)
    );
}

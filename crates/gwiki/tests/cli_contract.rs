mod common;

use std::path::PathBuf;

use gobby_wiki::{
    ScopeIdentity,
    output::{SearchOutput, SearchResultOutput, SearchResultType, SearchSourceExplanationOutput},
};
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
fn contract_declares_exit_code_table() {
    let contract = serde_json::to_value(gobby_wiki::contract::contract()).expect("contract json");
    let codes: Vec<u8> = contract["exit_codes"]
        .as_array()
        .expect("exit_codes array")
        .iter()
        .map(|entry| {
            u8::try_from(entry["code"].as_u64().expect("exit code")).expect("u8 exit code")
        })
        .collect();
    assert_eq!(codes, vec![0, 1, 2]);
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
        "ai",
    ];

    assert_eq!(compile.json_output_keys, expected_keys);
}

fn command<'a>(contract: &'a Value, name: &str) -> &'a Value {
    contract["commands"]
        .as_array()
        .expect("commands array")
        .iter()
        .find(|command| command["name"] == name)
        .unwrap_or_else(|| panic!("{name} command contract"))
}

fn assert_classification(
    command: &Value,
    daemon_consumed: bool,
    hard_dependencies: Value,
    optional_dependencies: Value,
    degradation: Value,
) {
    assert_eq!(command["daemon_consumed"], daemon_consumed);
    assert_eq!(command["hard_dependencies"], hard_dependencies);
    assert_eq!(command["optional_dependencies"], optional_dependencies);
    assert_eq!(command["multimodal"], "none");
    assert_eq!(command["degradation"], degradation);
}

fn representative_search_hit() -> SearchResultOutput {
    SearchResultOutput {
        title: Some("Contract guardrails".to_string()),
        fusion_key: "wiki:contract-guardrails".to_string(),
        wiki_page: PathBuf::from("knowledge/topics/contract-guardrails.md"),
        source_path: PathBuf::from("crates/gwiki/src/contract.rs"),
        result_type: SearchResultType::Code,
        snippet: "Contracts must describe serialized command output.".to_string(),
        score: 0.98,
        sources: vec!["fts".to_string(), "semantic".to_string()],
        explanations: vec![SearchSourceExplanationOutput {
            source: "fts".to_string(),
            rank: 1,
            score: 0.91,
        }],
    }
}

#[test]
fn search_contract_keys_serialize_from_representative_output() {
    let contract = gobby_wiki::contract::contract();
    let search = contract
        .commands
        .iter()
        .find(|command| command.name == "search")
        .expect("search command contract");
    let mut search_output = SearchOutput::new(
        ScopeIdentity::topic("contract-guardrails"),
        "contract keys",
        5,
        vec![representative_search_hit()],
        vec!["semantic_unavailable".to_string()],
    );
    search_output.hint = Some("Narrow the query to reduce token pressure.".to_string());
    let output = serde_json::to_value(search_output).expect("representative search output JSON");

    let missing_key = missing_json_output_key(&search.json_output_keys, &output);
    assert!(
        missing_key.is_none(),
        "command `{}` declares json_output_key `{}`, but representative output does not serialize \
         that key:\n{}",
        search.name,
        missing_key.unwrap_or("<none>"),
        serde_json::to_string_pretty(&output).expect("serialize output context")
    );
}

fn missing_json_output_key<'a>(keys: &'a [&str], output: &Value) -> Option<&'a str> {
    keys.iter()
        .copied()
        .find(|key| !json_contains_key(output, key))
}

fn json_contains_key(value: &Value, key: &str) -> bool {
    match value {
        Value::Object(object) => {
            object.contains_key(key) || object.values().any(|value| json_contains_key(value, key))
        }
        Value::Array(values) => values.iter().any(|value| json_contains_key(value, key)),
        _ => false,
    }
}

#[test]
fn parity_contract_tracks_code_grounding_and_dependency_classification() {
    let contract = pinned_contract();
    assert_eq!(contract["contract_version"], 18);
    assert!(
        contract["commands"]
            .as_array()
            .expect("commands array")
            .iter()
            .all(|command| command["name"] != "setup"),
        "standalone setup command must be absent from gwiki contract"
    );

    let code = command(&contract, "code");
    assert_eq!(code["positionals"], serde_json::json!([]));
    assert_eq!(
        code["flags"]
            .as_array()
            .expect("code flags")
            .iter()
            .map(|flag| flag["name"].as_str().expect("flag name"))
            .collect::<Vec<_>>(),
        vec![
            "--out",
            "--purge",
            "--force",
            "--scope",
            "--complete-scope",
            "--no-ai",
            "--ai-depth",
            "--ai-aggregate-profile",
            "--ai-aggregate-candidate",
            "--ai-verify-profile",
            "--ai-verify-scope",
            "--ai-prose-depth",
            "--ai-register",
            "--edge-limit",
            "--include-docs",
            "--since",
            "--compare-to",
            "--max-workers",
            "--repair-citations",
            "--allow-stale",
        ]
    );
    assert_classification(
        code,
        false,
        serde_json::json!(["PostgreSQL", "vault"]),
        serde_json::json!(["FalkorDB", "model synthesis"]),
        serde_json::json!({
            "output_shape": "graph outages fall back to indexed facts; AI off or failed writes structural pages",
            "metadata_keys": ["degraded_pages[]"]
        }),
    );

    assert!(
        contract["commands"]
            .as_array()
            .expect("commands array")
            .iter()
            .all(|command| command["name"] != "ask"),
        "retired ask command must be absent from gwiki contract"
    );

    let graph_context = command(&contract, "graph-context");

    assert_classification(
        graph_context,
        true,
        serde_json::json!(["PostgreSQL"]),
        serde_json::json!(["FalkorDB", "shared code graph"]),
        serde_json::json!({
            "output_shape": "wiki-link-only neighborhood",
            "metadata_keys": [
                "warnings[]",
                "degradation.degraded",
                "degradation.degraded_sources[]",
                "degradation.truncated",
                "degradation.truncated_components[]"
            ]
        }),
    );

    assert_eq!(
        graph_context["json_output_keys"],
        serde_json::json!([
            "command",
            "scope",
            "context",
            "source_bundle",
            "code_edges",
            "code_citations",
            "trust",
            "freshness",
            "audit",
            "warnings",
            "degradation"
        ])
    );

    assert!(
        !contract["commands"]
            .as_array()
            .expect("commands array")
            .iter()
            .any(|command| command["name"] == "research"),
        "research command must be absent from the contract"
    );

    let librarian = command(&contract, "librarian");
    assert_classification(
        librarian,
        true,
        serde_json::json!(["PostgreSQL", "vault"]),
        serde_json::json!(["FalkorDB/code graph", "Qdrant+embeddings", "model"]),
        serde_json::json!({
            "output_shape": "each check skipped independently with a note",
            "metadata_keys": ["checks[].available"]
        }),
    );

    let review_report = command(&contract, "review-report");
    assert_classification(
        review_report,
        true,
        serde_json::json!(["PostgreSQL", "change set"]),
        serde_json::json!(["FalkorDB/code graph and analytics"]),
        serde_json::json!({
            "output_shape": "report without risky-shift section",
            "metadata_keys": ["degraded", "degraded_sources[]"]
        }),
    );

    let citation_quality = command(&contract, "citation-quality");
    assert_classification(
        citation_quality,
        true,
        serde_json::json!(["PostgreSQL"]),
        serde_json::json!(["credibility signals", "model contradiction detection"]),
        serde_json::json!({
            "output_shape": "per-section skipped with a note",
            "metadata_keys": [
                "sections.credibility.available",
                "sections.coverage_gaps.available",
                "sections.contradictions.available",
                "sections.stale_sources.available",
                "sections.confidence.available"
            ]
        }),
    );
    assert_eq!(
        citation_quality["json_output_keys"],
        serde_json::json!([
            "command",
            "scope",
            "artifact_path",
            "dependencies",
            "sections",
            "markdown"
        ])
    );
}

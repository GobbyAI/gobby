mod common;

use std::path::PathBuf;

use gobby_wiki::{
    ScopeIdentity,
    output::{
        AskAiOutput, AskOutput, AskRelatedPageOutput, AskSynthesisOutput, SearchResultOutput,
        SearchSourceExplanationOutput,
    },
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
fn ask_contract_keys_serialize_from_representative_output() {
    let contract = gobby_wiki::contract::contract();
    let ask = contract
        .commands
        .iter()
        .find(|command| command.name == "ask")
        .expect("ask command contract");
    let output =
        serde_json::to_value(representative_ask_output()).expect("representative ask output JSON");

    let missing_key = missing_json_output_key(&ask.json_output_keys, &output);
    assert!(
        missing_key.is_none(),
        "command `{}` declares json_output_key `{}`, but representative output does not serialize \
         that key:\n{}",
        ask.name,
        missing_key.unwrap_or("<none>"),
        serde_json::to_string_pretty(&output).expect("serialize output context")
    );
}

fn representative_ask_output() -> AskOutput {
    AskOutput {
        command: "ask",
        scope: ScopeIdentity::topic("contract-guardrails"),
        query: "How do contract keys stay honest?".to_string(),
        status: "answered",
        hits: vec![SearchResultOutput {
            title: Some("Contract guardrails".to_string()),
            fusion_key: "wiki:contract-guardrails".to_string(),
            wiki_page: PathBuf::from("wiki/contract-guardrails.md"),
            source_path: PathBuf::from("crates/gwiki/src/contract.rs"),
            snippet: "Contracts must describe serialized command output.".to_string(),
            score: 0.98,
            sources: vec!["fts".to_string(), "semantic".to_string()],
            explanations: vec![SearchSourceExplanationOutput {
                source: "fts".to_string(),
                rank: 1,
                score: 0.91,
            }],
        }],
        related_pages: vec![AskRelatedPageOutput {
            title: Some("CLI contracts".to_string()),
            path: PathBuf::from("wiki/cli-contracts.md"),
            score: 0.72,
        }],
        sources: vec!["wiki/contract-guardrails.md".to_string()],
        gaps: vec!["No daemon fixture covered this output shape.".to_string()],
        stale_candidates: vec!["wiki/old-contract-keys.md".to_string()],
        suggested_questions: vec!["Which commands publish json_output_keys?".to_string()],
        warnings: vec!["semantic search degraded".to_string()],
        ai: Some(AskAiOutput {
            requested: true,
            requested_mode: "auto",
            route: "local",
            status: "degraded",
            model: Some("test-model".to_string()),
            error: Some("synthetic warning".to_string()),
        }),
        synthesis: Some(AskSynthesisOutput {
            answer: "Contract keys must serialize from command output.".to_string(),
            model: Some("test-model".to_string()),
        }),
    }
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

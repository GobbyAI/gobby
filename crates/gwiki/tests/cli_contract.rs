mod common;

use std::path::PathBuf;

use gobby_wiki::{
    ScopeIdentity,
    output::{
        AskAiOutput, AskCitationCheckOutput, AskEvidenceOutput, AskOutput, AskSynthesisOutput,
        CodeCitationOutput, SearchOutput, SearchResultOutput, SearchResultType,
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
    hard_dependencies: Value,
    optional_dependencies: Value,
    degradation: Value,
) {
    assert_eq!(command["hard_dependencies"], hard_dependencies);
    assert_eq!(command["optional_dependencies"], optional_dependencies);
    assert_eq!(command["multimodal"], "none");
    assert_eq!(command["degradation"], degradation);
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
        degraded: false,
        degraded_sources: Vec::new(),
        hits: vec![representative_search_hit()],
        sources: vec!["knowledge/topics/contract-guardrails.md".to_string()],
        code_citations: vec![CodeCitationOutput {
            file: "crates/gwiki/src/output.rs".to_string(),
            line: Some(102),
            symbol: Some("AskOutput".to_string()),
        }],
        evidence: vec![AskEvidenceOutput {
            wiki_page: PathBuf::from("knowledge/topics/contract-guardrails.md"),
            source_path: PathBuf::from("crates/gwiki/src/contract.rs"),
            excerpt_chars: 51,
        }],
        prompt_token_budget: 12_000,
        prompt_tokens_estimated: 64,
        truncated: true,
        truncated_components: vec!["evidence".to_string()],
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
            citation_check: AskCitationCheckOutput {
                status: "unsupported_claims",
                checked_claims: 1,
                unsupported_claims: vec![
                    "Contract keys must serialize from command output.".to_string(),
                ],
            },
        }),
    }
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
    let output = serde_json::to_value(SearchOutput::new(
        ScopeIdentity::topic("contract-guardrails"),
        "contract keys",
        5,
        vec![representative_search_hit()],
        vec!["semantic_unavailable".to_string()],
    ))
    .expect("representative search output JSON");

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
    assert_eq!(contract["contract_version"], 5);

    let ask = command(&contract, "ask");
    assert_classification(
        ask,
        serde_json::json!(["PostgreSQL"]),
        serde_json::json!([
            "model synthesis",
            "Qdrant+embeddings",
            "FalkorDB graph boost"
        ]),
        serde_json::json!({
            "output_shape": "model off emits retrieval-only hits with grounded citations; signal loss falls back to BM25-only evidence",
            "metadata_keys": [
                "degraded",
                "degraded_sources[]",
                "truncated",
                "truncated_components[]"
            ]
        }),
    );
    assert_eq!(
        ask["json_output_keys"],
        serde_json::json!([
            "command",
            "scope",
            "query",
            "status",
            "degraded",
            "degraded_sources",
            "hits",
            "sources",
            "code_citations",
            "evidence",
            "prompt_token_budget",
            "prompt_tokens_estimated",
            "truncated",
            "truncated_components",
            "warnings",
            "ai",
            "synthesis"
        ])
    );

    let graph_context = command(&contract, "graph-context");

    assert_classification(
        graph_context,
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

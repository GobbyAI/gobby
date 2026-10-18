//! Pins the Rust pane matcher to the corpus the Python engine records (#24381, plan 2.12).
//!
//! `fixtures/pane_detection/corpus.json` holds each case's expected output as the Python
//! matcher produces it. These tests replay the same cases through `gobby_daemon`.

use std::path::Path;

use gobby_daemon::pane_detection::{ManifestSet, compile_manifest};
use serde_json::{Value, json};

const CORPUS: &str = include_str!("fixtures/pane_detection/corpus.json");

const BUNDLED: [(&str, &str); 5] = [
    (
        "agy",
        include_str!("../../../src/gobby/install/shared/detection/agy.toml"),
    ),
    (
        "claude",
        include_str!("../../../src/gobby/install/shared/detection/claude.toml"),
    ),
    (
        "codex",
        include_str!("../../../src/gobby/install/shared/detection/codex.toml"),
    ),
    (
        "droid",
        include_str!("../../../src/gobby/install/shared/detection/droid.toml"),
    ),
    (
        "grok",
        include_str!("../../../src/gobby/install/shared/detection/grok.toml"),
    ),
];

const BUNDLED_RULE_COUNTS: [(&str, usize); 5] = [
    ("agy", 16),
    ("claude", 17),
    ("codex", 17),
    ("droid", 17),
    ("grok", 15),
];

fn bundled_text(provider: &str) -> &'static str {
    BUNDLED
        .iter()
        .find(|(id, _)| *id == provider)
        .map(|(_, text)| *text)
        .unwrap_or_else(|| panic!("unknown bundled provider {provider}"))
}

fn corpus_cases() -> Vec<Value> {
    let document: Value = serde_json::from_str(CORPUS).expect("corpus.json parses");
    document["cases"]
        .as_array()
        .expect("corpus cases is an array")
        .clone()
}

fn case_content(case: &Value) -> String {
    if let Some(provider) = case["provider"].as_str() {
        return bundled_text(provider).to_owned();
    }
    if let Some(path) = case["manifest"].as_str() {
        let full = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .join(path);
        return std::fs::read_to_string(&full)
            .unwrap_or_else(|error| panic!("read {}: {error}", full.display()));
    }
    case["content"]
        .as_str()
        .expect("inline case carries content")
        .to_owned()
}

fn observe(case: &Value) -> Value {
    let compiled = match compile_manifest(&case_content(case)) {
        Ok(compiled) => compiled,
        Err(error) => return json!({ "refusal": error.message() }),
    };
    let evaluation = compiled.detect(case["pane"].as_str().expect("matcher case carries a pane"));
    let (state, rule_id, reason) = match &evaluation.matched {
        Some(found) => (
            json!(found.state.as_str()),
            json!(found.rule_id),
            json!(found.reason),
        ),
        None => (Value::Null, Value::Null, Value::Null),
    };
    let issues: Vec<Value> = evaluation
        .issues
        .iter()
        .map(|issue| {
            json!({
                "rule_id": issue.rule_id,
                "code": issue.code.as_str(),
                "pattern": issue.pattern,
            })
        })
        .collect();
    json!({
        "state": state,
        "rule_id": rule_id,
        "reason": reason,
        "issues": issues,
    })
}

#[test]
fn matcher_matches_python_corpus() {
    for case in corpus_cases() {
        let id = case["id"].as_str().expect("case id");
        assert_eq!(observe(&case), case["expect"], "case {id}");
    }
}

#[test]
fn bundled_manifests_compile() {
    for (provider, rule_count) in BUNDLED_RULE_COUNTS {
        let compiled = compile_manifest(bundled_text(provider))
            .unwrap_or_else(|error| panic!("{provider} refused: {}", error.message()));
        assert!(
            compiled.issues().is_empty(),
            "{provider} carries issues: {:?}",
            compiled.issues()
        );
        assert_eq!(
            compiled.rule_ids().len(),
            rule_count,
            "{provider} rule count"
        );
    }

    let rows: Vec<(String, String)> = BUNDLED
        .iter()
        .map(|(id, text)| ((*id).to_owned(), (*text).to_owned()))
        .collect();
    let set = ManifestSet::from_rows(&rows);
    assert!(set.for_provider("droid").is_some());
    assert!(set.for_provider("unknown").is_none());
}

#[test]
fn manifest_refusals_and_issues() {
    let bad_id = compile_manifest(
        "id = \"Claude\"\nversion = \"1\"\nengine = 1\n\n[[rules]]\nid = \"ok\"\n\
         state = \"idle\"\npriority = 10\nregion = \"whole_recent\"\ncontains = [\"x\"]\n",
    );
    assert_eq!(
        bad_id.err().map(|error| error.message().to_owned()),
        Some("manifest id must be a lowercase provider identifier".to_owned()),
    );

    let oversize = "a".repeat(2049);
    let manifest = format!(
        "id = \"fixture\"\nversion = \"1\"\nengine = 1\n\n\
         [[rules]]\nid = \"oversize\"\nstate = \"working\"\npriority = 500\n\
         region = \"whole_recent\"\nline_regex = [\"{oversize}\"]\n\n\
         [[rules]]\nid = \"fallback\"\nstate = \"idle\"\npriority = 10\n\
         region = \"whole_recent\"\ncontains = [\"ready\"]\n"
    );
    let compiled = compile_manifest(&manifest).expect("oversize pattern drops, not refuses");
    assert_eq!(compiled.issues().len(), 1);
    assert_eq!(compiled.issues()[0].rule_id, "oversize");
    assert_eq!(compiled.issues()[0].pattern, oversize);
    assert!(
        compiled
            .match_rule("oversize", &"a".repeat(2048))
            .matched
            .is_none()
    );

    let runaway = "a".repeat(200) + "! ready";
    let manifest = "id = \"fixture\"\nversion = \"1\"\nengine = 1\n\n\
         [[rules]]\nid = \"runaway\"\nstate = \"working\"\npriority = 100\n\
         region = \"whole_recent\"\nline_regex = [\"(a+)+$(?<=!)\"]\n\n\
         [[rules]]\nid = \"fallback\"\nstate = \"idle\"\npriority = 50\n\
         region = \"whole_recent\"\ncontains = [\"ready\"]\n";
    let evaluation = compile_manifest(manifest)
        .expect("backtracking pattern compiles")
        .detect(&runaway);
    assert_eq!(
        evaluation.matched.map(|found| found.rule_id),
        Some("fallback".to_owned()),
    );
    assert_eq!(evaluation.issues.len(), 1);
    assert_eq!(evaluation.issues[0].rule_id, "runaway");
    assert_eq!(evaluation.issues[0].code.as_str(), "pattern_timeout");
}

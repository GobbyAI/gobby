//! Pins the Rust pane matcher to the corpus the Python engine records (#24381, plan 2.12).
//!
//! `fixtures/pane_detection/corpus.json` holds each case's expected output as the Python
//! matcher produces it. These tests replay the same cases through `gobby_daemon`.

use std::path::Path;

use gobby_daemon::pane_detection::{
    AttentionInput, CompiledManifest, IdleDetector, LoopTracker, ManifestSet, PromptDetector,
    RunDetectState, StallClassifier, StallRunState, capacity_pane_message, classify_attention,
    compile_manifest, detect_provider_quota,
};
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

fn detector_cases(prefix: &str) -> Vec<Value> {
    let document: Value = serde_json::from_str(CORPUS).expect("corpus.json parses");
    document["detectors"]
        .as_array()
        .expect("corpus detectors is an array")
        .iter()
        .filter(|case| {
            case["detector"]
                .as_str()
                .is_some_and(|name| name.starts_with(prefix))
        })
        .cloned()
        .collect()
}

fn bundled_manifests() -> ManifestSet {
    let rows: Vec<(String, String)> = BUNDLED
        .iter()
        .map(|(provider, text)| ((*provider).to_owned(), (*text).to_owned()))
        .collect();
    ManifestSet::from_rows(&rows)
}

fn stall_state(start: &Value) -> StallRunState {
    StallRunState {
        consecutive_provider_hits: start["consecutive_hits"]
            .as_u64()
            .map_or(0, |hits| u32::try_from(hits).expect("hit count fits")),
        last_check_at: start["last_check_at"].as_f64().unwrap_or(0.0),
    }
}

fn observe_prompt(manifest: Option<&CompiledManifest>, case: &Value) -> Value {
    let prompt = PromptDetector::new(manifest);
    let pane = case["pane"].as_str().expect("prompt case carries a pane");
    match case["detector"].as_str().expect("detector name") {
        "prompt.trust" => json!(prompt.detect_trust_prompt(pane)),
        "prompt.trust_keys" => json!(prompt.trust_dismiss_keys(pane)),
        "prompt.loop" => json!(prompt.detect_loop_prompt(pane)),
        "prompt.approval" => json!(prompt.detect_approval_prompt(pane)),
        "prompt.queued_message" => json!(prompt.detect_queued_message_prompt(pane)),
        "prompt.queued_continuation" => json!(prompt.detect_queued_continuation_prompt(pane)),
        "prompt.detect" => prompt
            .detect_prompt(pane)
            .map_or(Value::Null, |found| found.to_payload()),
        other => panic!("not a prompt detector: {other}"),
    }
}

fn observe_idle(manifest: Option<&CompiledManifest>, case: &Value) -> Value {
    let idle = IdleDetector::new(manifest);
    let pane = case["pane"].as_str();
    match case["detector"].as_str().expect("detector name") {
        "idle.detect" => json!(idle.detect(pane.expect("idle.detect carries a pane"))),
        "idle.composer" => {
            let read = idle.composer_read(pane);
            json!({"state": read.state.as_str(), "line": read.line})
        }
        "idle.unsubmitted" => json!(
            idle.unsubmitted_input_fingerprint(pane.expect("idle.unsubmitted carries a pane"))
        ),
        "idle.turn" => {
            json!(idle.turn_in_flight_fingerprint(pane.expect("idle.turn carries a pane")))
        }
        other => panic!("not an idle detector: {other}"),
    }
}

fn observe_stall(manifest: Option<&CompiledManifest>, case: &Value) -> Value {
    let classifier = StallClassifier::new(manifest);
    let mut state = stall_state(&case["state"]);
    let steps: Vec<Value> = case["steps"]
        .as_array()
        .expect("stall case carries steps")
        .iter()
        .map(|step| {
            let at = step["at"].as_f64().expect("stall step carries a time");
            let verdict = classifier.classify(
                &mut state,
                at,
                step["pane"].as_str(),
                step["error"].as_str(),
            );
            json!({
                "status": verdict.status.as_str(),
                "reason": verdict.reason,
                "consecutive_hits": verdict.consecutive_hits,
            })
        })
        .collect();
    Value::Array(steps)
}

fn observe_attention(manifests: &ManifestSet, case: &Value) -> Value {
    let provider = case["provider"]
        .as_str()
        .expect("attention case names a provider");
    let manifest = manifests.for_provider(provider);
    let mut state = RunDetectState::new();
    state.stall = stall_state(&case["state"]["stall"]);
    if case["state"]["trust_dismissed"].as_bool() == Some(true) {
        state.mark_trust_dismissed();
    }
    if let Some(pane) = case["state"]["approval_dismissed_pane"].as_str() {
        state.mark_approval_prompt_dismissed(pane);
    }
    let input = AttentionInput {
        pane: case["pane"]
            .as_str()
            .expect("attention case carries a pane"),
        error: case["error"].as_str(),
        now: case["now"].as_f64().unwrap_or(100.0),
        auto_enter: case["auto_enter"].as_bool().unwrap_or(true),
    };
    let classification = classify_attention(
        &PromptDetector::new(manifest),
        &StallClassifier::new(manifest),
        &mut state,
        &input,
    );
    let transition = classification.transition.map(|found| {
        json!({
            "reason": found.reason,
            "kind": found.kind,
            "fingerprint": found.fingerprint,
            "payload": found.payload.to_payload(),
        })
    });
    json!({"transition": transition, "dismissed": classification.dismissed})
}

fn observe_loop(case: &Value) -> Value {
    let threshold = case["threshold"]
        .as_u64()
        .map_or(3, |value| u32::try_from(value).expect("threshold fits"));
    let tracker = LoopTracker::new(threshold).expect("corpus thresholds are at least one");
    let mut state = RunDetectState::new();
    let dismissals = case["dismissals"]
        .as_u64()
        .expect("loop case counts dismissals");
    let steps: Vec<Value> = (0..dismissals)
        .map(|_| {
            let count = tracker.record_dismissal(&mut state);
            json!({"count": count, "escalate": tracker.should_escalate(&state)})
        })
        .collect();
    Value::Array(steps)
}

fn observe_quota(case: &Value) -> Value {
    let provider = case["provider"]
        .as_str()
        .expect("quota case names a provider");
    match case["detector"].as_str().expect("detector name") {
        "quota.detect" => detect_provider_quota(
            case["pane"].as_str().expect("quota.detect carries a pane"),
            provider,
        )
        .map_or(
            Value::Null,
            |hit| json!({"provider": hit.provider, "reset_time": hit.reset_time}),
        ),
        "quota.capacity" => json!(capacity_pane_message(provider)),
        other => panic!("not a quota detector: {other}"),
    }
}

/// Replays one corpus detector case through the bound provider manifest.
fn observe_detector(manifests: &ManifestSet, case: &Value) -> Value {
    let name = case["detector"]
        .as_str()
        .expect("detector case names a detector");
    let provider = case["provider"]
        .as_str()
        .expect("detector case names a provider");
    let manifest = manifests.for_provider(provider);
    if name.starts_with("prompt.") {
        observe_prompt(manifest, case)
    } else if name.starts_with("idle.") {
        observe_idle(manifest, case)
    } else if name == "stall.classify" {
        observe_stall(manifest, case)
    } else if name == "attention.classify" {
        observe_attention(manifests, case)
    } else if name == "loop.dismiss" {
        observe_loop(case)
    } else {
        observe_quota(case)
    }
}

fn assert_detector_cases(prefix: &str) {
    let manifests = bundled_manifests();
    let cases = detector_cases(prefix);
    assert!(!cases.is_empty(), "corpus has {prefix} cases");
    for case in cases {
        let id = case["id"].as_str().expect("case id");
        assert_eq!(
            observe_detector(&manifests, &case),
            case["expect"],
            "case {id}"
        );
    }
}

#[test]
fn prompt_detector_matches_python() {
    assert_detector_cases("prompt.");
}

#[test]
fn idle_detector_matches_python() {
    assert_detector_cases("idle.");
}

#[test]
fn stall_and_attention_match_python() {
    assert_detector_cases("stall.");
    assert_detector_cases("attention.");
}

#[test]
fn loop_and_quota_match_python() {
    assert_detector_cases("loop.");
    assert_detector_cases("quota.");
}

/// Python's `int()` reads a Unicode digit in an option number, so `1\u{661}.` is option 11.
#[test]
fn prompt_options_read_unicode_digits_as_python() {
    let manifests = bundled_manifests();
    let prompt = PromptDetector::new(manifests.for_provider("claude"))
        .detect_prompt("Pick one:\n1\u{661}. Eleven\n2. Two\n")
        .expect("two enumerated options make a question");
    assert_eq!(
        prompt.to_payload()["options"],
        json!([
            {"option": 2, "label": "Two"},
            {"option": 11, "label": "Eleven"}
        ])
    );
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

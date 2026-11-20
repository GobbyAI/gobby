//! Regex audit, Rust side: the two-engine driver and the fixture checks.
//!
//! The driver is `run_work_file`. The Python audit writes a work file of generated pattern
//! texts, one-character atoms and boundary atoms, runs the ignored `run_generated_patterns`
//! test through cargo, and compares the result file with the Python observable of each
//! original pattern. The driver asserts nothing about the results itself.
//!
//! Work file (JSON object, three lists):
//! - `patterns`: `{"index", "text", "flags": {"case_insensitive", "multi_line"}, "mode",
//!   "probes"}`; mode is `search`, `match`, `fullmatch` or `sub`, and a `sub` entry also has a
//!   literal `replacement`. An entry that breaks this shape gets `{"index", "error": "bad entry:
//!   ..."}` and is not run.
//! - `atoms`: `{"index", "text"}`, measured over every Unicode scalar value.
//! - `boundaries`: `{"index", "text", "subjects"}`.
//!
//! Result file (JSON object, the same three lists, each entry carrying its `index`):
//! - a pattern entry has `error` (a refused entry or a compile error) or `results`, one per
//!   probe: `{"probe", "matched", "span", "groups"}` for the three single-match modes and
//!   `{"probe", "matches": [{"span", "groups"}], "replaced"}` for `sub`;
//! - an atom entry has `error` or `ranges`, the sorted inclusive scalar ranges it matches;
//! - a boundary entry has `error` or `offsets`, a map from subject to the offsets at which the
//!   atom holds.
//!
//! Every offset is a character index, as Python reports it. A group that did not take part in
//! a match is JSON null.
//!
//! The fixture checks read `tests/contracts/regex_audit/*.json` and pin the synthetic
//! exemplars on the Rust engine. They prove no generated pattern; the driver run does.

use std::path::{Path, PathBuf};

use regex::{NoExpand, Regex, RegexBuilder};
use serde_json::{Map, Value, json};

const WORK_ENV: &str = "GOBBY_REGEX_AUDIT_WORK";
const OUT_ENV: &str = "GOBBY_REGEX_AUDIT_OUT";
const FLAG_KEYS: [&str; 2] = ["case_insensitive", "multi_line"];
const MODES: [&str; 4] = ["search", "match", "fullmatch", "sub"];

// ---------------------------------------------------------------------------------------------
// Driver
// ---------------------------------------------------------------------------------------------

/// Run every entry of the work file at `work` and write the result file at `out`.
///
/// Returns `Err` only for a work file that cannot be read or parsed, or a result file that
/// cannot be written. A malformed entry is refused in its own result.
pub fn run_work_file(work: &Path, out: &Path) -> Result<(), String> {
    let text = std::fs::read_to_string(work)
        .map_err(|error| format!("work file {} is unreadable: {error}", work.display()))?;
    let document: Value = serde_json::from_str(&text)
        .map_err(|error| format!("work file {} is not JSON: {error}", work.display()))?;
    let Some(object) = document.as_object() else {
        return Err(format!("work file {} is not a JSON object", work.display()));
    };
    let patterns = entries(object, "patterns")?
        .iter()
        .map(run_pattern_entry)
        .collect::<Vec<_>>();
    let atoms = entries(object, "atoms")?
        .iter()
        .map(run_atom_entry)
        .collect::<Vec<_>>();
    let boundaries = entries(object, "boundaries")?
        .iter()
        .map(run_boundary_entry)
        .collect::<Vec<_>>();
    let result = json!({"patterns": patterns, "atoms": atoms, "boundaries": boundaries});
    let rendered = serde_json::to_string(&result)
        .map_err(|error| format!("result file could not be rendered: {error}"))?;
    std::fs::write(out, rendered)
        .map_err(|error| format!("result file {} is unwritable: {error}", out.display()))
}

fn entries<'a>(object: &'a Map<String, Value>, key: &str) -> Result<&'a [Value], String> {
    match object.get(key) {
        None => Ok(&[]),
        Some(Value::Array(items)) => Ok(items.as_slice()),
        Some(_) => Err(format!("work file key {key} is not a list")),
    }
}

fn entry_index(entry: &Value) -> Value {
    entry.get("index").cloned().unwrap_or(Value::Null)
}

fn refused(entry: &Value, what: &str) -> Value {
    json!({"index": entry_index(entry), "error": format!("bad entry: {what}")})
}

fn failed(entry: &Value, error: String) -> Value {
    json!({"index": entry_index(entry), "error": error})
}

/// The flags of a pattern entry: exactly the two boolean keys, nothing else.
fn entry_flags(entry: &Value) -> Result<(bool, bool), String> {
    let Some(flags) = entry.get("flags") else {
        return Err("no flags".to_owned());
    };
    let Some(flags) = flags.as_object() else {
        return Err("flags is not an object".to_owned());
    };
    for key in flags.keys() {
        if !FLAG_KEYS.contains(&key.as_str()) {
            return Err(format!("unknown flag {key}"));
        }
    }
    let flag = |name: &str| -> Result<bool, String> {
        match flags.get(name) {
            Some(Value::Bool(value)) => Ok(*value),
            Some(_) => Err(format!("flag {name} is not a boolean")),
            None => Err(format!("flag {name} is missing")),
        }
    };
    Ok((flag("case_insensitive")?, flag("multi_line")?))
}

struct PatternEntry<'a> {
    text: &'a str,
    case_insensitive: bool,
    multi_line: bool,
    mode: &'a str,
    probes: Vec<&'a str>,
    replacement: Option<&'a str>,
}

fn parse_pattern_entry(entry: &Value) -> Result<PatternEntry<'_>, String> {
    if !entry.get("index").is_some_and(Value::is_u64) {
        return Err("index is not a non-negative integer".to_owned());
    }
    let Some(text) = entry.get("text").and_then(Value::as_str) else {
        return Err("text is not a string".to_owned());
    };
    let (case_insensitive, multi_line) = entry_flags(entry)?;
    let Some(mode) = entry.get("mode").and_then(Value::as_str) else {
        return Err("mode is not a string".to_owned());
    };
    if !MODES.contains(&mode) {
        return Err(format!("unknown mode {mode}"));
    }
    let Some(probes) = entry.get("probes").and_then(Value::as_array) else {
        return Err("probes is not a list".to_owned());
    };
    let mut texts = Vec::with_capacity(probes.len());
    for probe in probes {
        let Some(probe) = probe.as_str() else {
            return Err("a probe is not a string".to_owned());
        };
        texts.push(probe);
    }
    let replacement = match (mode, entry.get("replacement")) {
        ("sub", Some(Value::String(replacement))) => Some(replacement.as_str()),
        ("sub", _) => return Err("sub without a string replacement".to_owned()),
        (_, None) => None,
        (_, Some(_)) => return Err(format!("replacement given for mode {mode}")),
    };
    Ok(PatternEntry {
        text,
        case_insensitive,
        multi_line,
        mode,
        probes: texts,
        replacement,
    })
}

/// The text the engine compiles for a mode: `match` and `fullmatch` are anchored searches.
fn mode_text(text: &str, mode: &str) -> String {
    match mode {
        "match" => format!(r"\A(?:{text})"),
        "fullmatch" => format!(r"\A(?:{text})\z"),
        _ => text.to_owned(),
    }
}

fn run_pattern_entry(entry: &Value) -> Value {
    let parsed = match parse_pattern_entry(entry) {
        Ok(parsed) => parsed,
        Err(what) => return refused(entry, &what),
    };
    let compiled = RegexBuilder::new(&mode_text(parsed.text, parsed.mode))
        .case_insensitive(parsed.case_insensitive)
        .multi_line(parsed.multi_line)
        .build();
    let regex = match compiled {
        Ok(regex) => regex,
        Err(error) => return failed(entry, format!("compile error: {error}")),
    };
    let results = parsed
        .probes
        .iter()
        .map(|probe| match parsed.replacement {
            Some(replacement) => sub_result(&regex, probe, replacement),
            None => single_match_result(&regex, probe),
        })
        .collect::<Vec<_>>();
    json!({"index": entry_index(entry), "results": results})
}

/// The character index of a byte offset of `text`.
fn char_index(text: &str, byte: usize) -> usize {
    text[..byte].chars().count()
}

fn span(text: &str, start: usize, end: usize) -> Value {
    json!([char_index(text, start), char_index(text, end)])
}

/// One entry per capture group in group order; null for a group that did not take part.
fn group_spans(text: &str, captures: &regex::Captures<'_>) -> Value {
    Value::Array(
        (1..captures.len())
            .map(|group| match captures.get(group) {
                Some(found) => span(text, found.start(), found.end()),
                None => Value::Null,
            })
            .collect(),
    )
}

fn single_match_result(regex: &Regex, probe: &str) -> Value {
    match regex.captures(probe) {
        Some(captures) => {
            let whole = captures
                .get(0)
                .map_or(Value::Null, |found| span(probe, found.start(), found.end()));
            json!({"probe": probe, "matched": true, "span": whole,
                   "groups": group_spans(probe, &captures)})
        }
        None => json!({"probe": probe, "matched": false, "span": Value::Null, "groups": []}),
    }
}

/// Every match the substitution replaces, in order, and the text after replacing all of them.
fn sub_matches(regex: &Regex, probe: &str) -> Vec<Value> {
    regex
        .captures_iter(probe)
        .map(|captures| {
            let whole = captures
                .get(0)
                .map_or(Value::Null, |found| span(probe, found.start(), found.end()));
            json!({"span": whole, "groups": group_spans(probe, &captures)})
        })
        .collect()
}

fn sub_result(regex: &Regex, probe: &str, replacement: &str) -> Value {
    let replaced = regex.replace_all(probe, NoExpand(replacement)).into_owned();
    json!({"probe": probe, "matches": sub_matches(regex, probe), "replaced": replaced})
}

/// The inclusive scalar ranges a one-character atom matches as a whole string.
fn atom_ranges(text: &str) -> Result<Vec<(u32, u32)>, String> {
    let regex = Regex::new(&format!(r"\A(?:{text})\z"))
        .map_err(|error| format!("compile error: {error}"))?;
    let mut ranges: Vec<(u32, u32)> = Vec::new();
    let mut buffer = [0_u8; 4];
    for character in (0..=0x10FFFF_u32).filter_map(char::from_u32) {
        if !regex.is_match(character.encode_utf8(&mut buffer)) {
            continue;
        }
        let point = u32::from(character);
        match ranges.last_mut() {
            Some(last) if last.1 + 1 == point => last.1 = point,
            _ => ranges.push((point, point)),
        }
    }
    Ok(ranges)
}

fn run_atom_entry(entry: &Value) -> Value {
    let Some(text) = entry.get("text").and_then(Value::as_str) else {
        return refused(entry, "atom text is not a string");
    };
    match atom_ranges(text) {
        Ok(ranges) => {
            let rendered = ranges
                .iter()
                .map(|(low, high)| json!([low, high]))
                .collect::<Vec<_>>();
            json!({"index": entry_index(entry), "ranges": rendered})
        }
        Err(error) => failed(entry, error),
    }
}

/// The character offsets of `subject` at which the zero-width atom holds.
fn boundary_offsets(regex: &Regex, subject: &str) -> Vec<usize> {
    let mut starts = subject
        .char_indices()
        .map(|(byte, _)| byte)
        .collect::<Vec<_>>();
    starts.push(subject.len());
    starts
        .into_iter()
        .filter(|byte| {
            regex
                .find_at(subject, *byte)
                .is_some_and(|found| found.start() == *byte && found.end() == *byte)
        })
        .map(|byte| char_index(subject, byte))
        .collect()
}

fn run_boundary_entry(entry: &Value) -> Value {
    let Some(text) = entry.get("text").and_then(Value::as_str) else {
        return refused(entry, "boundary text is not a string");
    };
    let Some(subjects) = entry.get("subjects").and_then(Value::as_array) else {
        return refused(entry, "subjects is not a list");
    };
    let regex = match Regex::new(text) {
        Ok(regex) => regex,
        Err(error) => return failed(entry, format!("compile error: {error}")),
    };
    let mut offsets = Map::new();
    for subject in subjects {
        let Some(subject) = subject.as_str() else {
            return refused(entry, "a subject is not a string");
        };
        offsets.insert(subject.to_owned(), json!(boundary_offsets(&regex, subject)));
    }
    json!({"index": entry_index(entry), "offsets": offsets})
}

#[test]
// test-quality: allow UNCONDITIONAL_SKIP -- not a test: the driver entry the Python regex audit runs with --ignored and a generated work file
#[ignore = "run by the Python regex audit with a generated work file"]
fn run_generated_patterns() {
    let work = std::env::var(WORK_ENV).unwrap_or_default();
    let out = std::env::var(OUT_ENV).unwrap_or_default();
    assert!(
        !work.is_empty() && !out.is_empty(),
        "work file not given: set {WORK_ENV} and {OUT_ENV}"
    );
    let outcome = run_work_file(Path::new(&work), Path::new(&out));
    assert!(outcome.is_ok(), "driver failed: {outcome:?}");
}

// ---------------------------------------------------------------------------------------------
// Driver self-test (synthetic literals only)
// ---------------------------------------------------------------------------------------------

fn pattern(index: u64, text: &str, ci: bool, ml: bool, mode: &str, probes: &[&str]) -> Value {
    json!({"index": index, "text": text, "flags": {"case_insensitive": ci, "multi_line": ml},
           "mode": mode, "probes": probes})
}

fn sub_pattern(index: u64, text: &str, replacement: &str, probes: &[&str]) -> Value {
    let mut entry = pattern(index, text, false, false, "sub", probes);
    if let Some(object) = entry.as_object_mut() {
        object.insert("replacement".to_owned(), json!(replacement));
    }
    entry
}

fn run_in_test(work: &Value) -> Value {
    let dir = tempfile::tempdir().expect("a temporary directory for the work file");
    let work_path = dir.path().join("work.json");
    let out_path = dir.path().join("out.json");
    std::fs::write(&work_path, work.to_string()).expect("the work file is written");
    let outcome = run_work_file(&work_path, &out_path);
    assert!(
        outcome.is_ok(),
        "the driver failed on a readable work file: {outcome:?}"
    );
    let text = std::fs::read_to_string(&out_path).expect("the result file exists");
    serde_json::from_str(&text).expect("the result file is JSON")
}

fn by_index<'a>(results: &'a Value, list: &str, index: u64) -> &'a Value {
    let entries = results[list].as_array().expect("a result list");
    let found = entries
        .iter()
        .find(|entry| entry["index"].as_u64() == Some(index));
    assert!(found.is_some(), "no {list} result for index {index}");
    found.unwrap_or(&Value::Null)
}

fn probe_result(results: &Value, index: u64, probe: usize) -> &Value {
    let entry = by_index(results, "patterns", index);
    assert!(
        entry.get("error").is_none(),
        "entry {index} has an error: {}",
        entry["error"]
    );
    &entry["results"][probe]
}

#[test]
fn driver_entry_runs_an_in_test_work_file() {
    let work = json!({
        "patterns": [
            sub_pattern(0, "syn-([0-9]+)", "n", &["id syn-42 syn-7"]),
            pattern(1, "not slow", true, false, "search", &["It is NOT slow"]),
            pattern(2, "not slow", false, false, "search", &["It is NOT slow"]),
            pattern(3, "^b", false, true, "search", &["a\nb"]),
            pattern(4, "^b", false, false, "search", &["a\nb"]),
            json!({"index": 5, "text": "q", "mode": "search", "probes": ["q"]}),
            json!({"index": 6, "text": "q", "flags": {"multi_line": false}, "mode": "search",
                   "probes": ["q"]}),
            json!({"index": 7, "text": "q",
                   "flags": {"case_insensitive": false, "multi_line": false, "dot_all": false},
                   "mode": "search", "probes": ["q"]}),
            pattern(8, "q", false, false, "replace", &["q"]),
            pattern(9, "q", false, false, "sub", &["q"]),
            pattern(10, "a|ab", false, false, "fullmatch", &["ab", "abc"]),
            pattern(11, "b", false, false, "match", &["ab"]),
            pattern(12, "b", false, false, "search", &["ab"]),
            sub_pattern(13, "(a)?b", "n", &["b ab"]),
            pattern(14, "(a)?b", false, false, "search", &["b ab"]),
        ],
        "atoms": [{"index": 0, "text": "[a-c]"}],
        "boundaries": [
            {"index": 0, "text": r"\B", "subjects": ["", "!", "a!"]},
            {"index": 1, "text": r"\b", "subjects": [""]},
        ],
    });
    let results = run_in_test(&work);

    let two = probe_result(&results, 0, 0);
    assert_eq!(
        two["matches"],
        json!([{"span": [3, 9], "groups": [[7, 9]]}, {"span": [10, 15], "groups": [[14, 15]]}]),
        "a substitution reports every match with its group spans"
    );
    assert_eq!(
        two["replaced"],
        json!("id n n"),
        "replace_all replaces both matches"
    );

    assert_eq!(
        probe_result(&results, 1, 0)["span"],
        json!([6, 14]),
        "case_insensitive is set"
    );
    assert_eq!(
        probe_result(&results, 2, 0)["matched"],
        json!(false),
        "case_insensitive false leaves the search case sensitive"
    );
    assert_eq!(
        probe_result(&results, 3, 0)["span"],
        json!([2, 3]),
        "multi_line is set"
    );
    assert_eq!(
        probe_result(&results, 4, 0)["matched"],
        json!(false),
        "multi_line false leaves ^ at the start of the subject only"
    );

    for index in 5..=9 {
        let entry = by_index(&results, "patterns", index);
        let error = entry["error"].as_str().unwrap_or_default();
        assert!(
            error.starts_with("bad entry"),
            "malformed entry {index} is refused, got {entry}"
        );
        assert!(
            entry.get("results").is_none(),
            "refused entry {index} was run"
        );
    }

    let full = by_index(&results, "patterns", 10);
    assert_eq!(
        full["results"][0]["matched"],
        json!(true),
        "a|ab fullmatches ab"
    );
    assert_eq!(
        full["results"][0]["span"],
        json!([0, 2]),
        "the fullmatch covers ab"
    );
    assert_eq!(
        full["results"][1]["matched"],
        json!(false),
        "a|ab does not fullmatch abc"
    );
    assert_eq!(
        probe_result(&results, 11, 0)["matched"],
        json!(false),
        "match mode is anchored at the start"
    );
    assert_eq!(
        probe_result(&results, 12, 0)["span"],
        json!([1, 2]),
        "search is not anchored"
    );

    let absent = probe_result(&results, 13, 0);
    let matches = absent["matches"].as_array().expect("sub matches");
    assert_eq!(matches.len(), 2, "two substitutions, got {absent}");
    assert_eq!(matches[0]["span"], json!([0, 1]), "first match");
    let first_groups = matches[0]["groups"].as_array().expect("group list");
    assert_eq!(first_groups.len(), 1, "one entry per group");
    assert!(
        first_groups[0].is_null(),
        "an absent group is null, got {}",
        first_groups[0]
    );
    assert_eq!(matches[1]["span"], json!([2, 4]), "second match");
    assert_eq!(
        matches[1]["groups"],
        json!([[2, 3]]),
        "a present group has its span"
    );
    assert_eq!(
        absent["replaced"],
        json!("n n"),
        "both matches are replaced"
    );
    let single = probe_result(&results, 14, 0);
    assert_eq!(
        single["span"],
        json!([0, 1]),
        "search finds the first match"
    );
    let single_groups = single["groups"].as_array().expect("group list");
    assert_eq!(single_groups.len(), 1, "one entry per group");
    assert!(
        single_groups[0].is_null(),
        "an absent group is null in search mode"
    );

    assert_eq!(
        by_index(&results, "atoms", 0)["ranges"],
        json!([[97, 99]]),
        "a class atom's membership"
    );
    let non_word = &by_index(&results, "boundaries", 0)["offsets"];
    assert_eq!(non_word[""], json!([0]), r"\B holds on the empty subject");
    assert_eq!(non_word["!"], json!([0, 1]), r"\B on a nonword character");
    assert_eq!(non_word["a!"], json!([2]), r"\B after a word edge");
    let word = &by_index(&results, "boundaries", 1)["offsets"];
    assert_eq!(word[""], json!([]), r"\b never holds on the empty subject");

    let dir = tempfile::tempdir().expect("a temporary directory");
    let missing = run_work_file(
        &dir.path().join("absent.json"),
        &dir.path().join("out.json"),
    );
    assert!(missing.is_err(), "an unreadable work path is an error");
}

// ---------------------------------------------------------------------------------------------
// Fixture checks
// ---------------------------------------------------------------------------------------------

const FIXTURE_DIR: &str = "tests/contracts/regex_audit";

const REQUIRED_CONSTRUCT_IDS: &[&str] = &[
    "literal",
    "non-ascii-literal",
    "any",
    "class",
    "negated-class",
    "class-range",
    "start-caret",
    "capture-group",
    "non-capture-group",
    "alternation",
    "star",
    "plus",
    "question",
    "counted-repeat",
    "lazy-repeat",
    "space-shorthand",
    "not-space-shorthand",
    "digit-shorthand",
    "not-digit-shorthand",
    "word-shorthand",
    "not-word-shorthand",
    "word-boundary",
    "non-word-boundary",
    "inline-flag-i",
    "lookahead",
    "negative-lookahead",
    "lookbehind",
    "negative-lookbehind",
    "backreference",
    "named-backreference",
    "atomic",
    "possessive",
    "conditional",
    "end-of-string-Z",
    "end-of-string-z",
    "start-A",
    "escaped-angle",
    "escaped-punctuation",
    "bracket-in-class",
    "class-intersection",
    "class-difference",
    "class-symmetric",
    "literal-brace",
    "brace-comma-n",
    "named-group-P",
    "named-group-angle",
    "inline-flag-m",
    "inline-flag-s",
    "scoped-flag",
    "verbose-flag",
    "unicode-property",
    "posix-class",
    "lazy",
    "dollar",
    "multiline-dollar",
    "octal-escape",
    "hex-escape",
    "unicode-escape",
    "named-char",
    "class-backspace",
    "empty-alternative",
];

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn fixture(name: &str) -> Value {
    let relative = format!("{FIXTURE_DIR}/{name}");
    let path = repo_root().join(&relative);
    assert!(path.is_file(), "{relative} is missing");
    let text = std::fs::read_to_string(&path).expect("the fixture is readable");
    serde_json::from_str(&text).expect("the fixture is JSON")
}

fn list<'a>(value: &'a Value, key: &str) -> &'a [Value] {
    let items = value[key].as_array();
    assert!(items.is_some(), "fixture key {key} is not a list");
    items.map_or(&[], Vec::as_slice)
}

fn text_of<'a>(value: &'a Value, key: &str) -> &'a str {
    let text = value[key].as_str();
    assert!(
        text.is_some(),
        "fixture key {key} is not a string in {value}"
    );
    text.unwrap_or_default()
}

/// The Rust observable of a fixture probe: the truth of a search, or of a fullmatch for the
/// fullmatch consumers.
fn rust_truth(regex: &Regex, input: &str) -> bool {
    regex.is_match(input)
}

fn compile_for(text: &str, consumer: &str) -> Result<Regex, regex::Error> {
    let (mode, case_insensitive) = consumer_mode(consumer);
    RegexBuilder::new(&mode_text(text, mode))
        .case_insensitive(case_insensitive)
        .build()
}

fn consumer_mode(consumer: &str) -> (&'static str, bool) {
    match consumer {
        "FULLMATCH" | "FULLMATCH-PATH" => ("fullmatch", false),
        "MATCH-GROUPS-FIRSTLINE" => ("match", false),
        "SPAN-ALLOWLIST" => ("search", true),
        _ => ("search", false),
    }
}

/// The fancy-regex text of an original under a consumer's mode and external flags.
fn fancy_text(original: &str, consumer: &str) -> String {
    let (mode, case_insensitive) = consumer_mode(consumer);
    let text = mode_text(original, mode);
    if case_insensitive {
        format!("(?i:{text})")
    } else {
        text
    }
}

/// The Rust rendering of a counterexample's wrong text: `\Z` written as `\z`.
fn end_anchor_to_rust(text: &str) -> String {
    let mut rendered = String::with_capacity(text.len());
    let mut characters = text.chars();
    while let Some(character) = characters.next() {
        if character == '\\' {
            match characters.next() {
                Some('Z') => rendered.push_str(r"\z"),
                Some(next) => {
                    rendered.push('\\');
                    rendered.push(next);
                }
                None => rendered.push('\\'),
            }
        } else {
            rendered.push(character);
        }
    }
    rendered
}

fn exhaustive_inputs(alphabet: &str, max_len: usize) -> Vec<String> {
    let symbols = alphabet.chars().collect::<Vec<_>>();
    let mut all = vec![String::new()];
    let mut frontier = vec![String::new()];
    for _ in 0..max_len {
        let mut next = Vec::with_capacity(frontier.len() * symbols.len());
        for prefix in &frontier {
            for symbol in &symbols {
                let mut grown = prefix.clone();
                grown.push(*symbol);
                next.push(grown);
            }
        }
        all.extend(next.iter().cloned());
        frontier = next;
    }
    all
}

fn ranges_value(ranges: &[(u32, u32)]) -> Value {
    Value::Array(
        ranges
            .iter()
            .map(|(low, high)| json!([low, high]))
            .collect(),
    )
}

fn cut_to(ranges: &[(u32, u32)], low: u32, high: u32) -> Vec<(u32, u32)> {
    ranges
        .iter()
        .filter(|(start, end)| *end >= low && *start <= high)
        .map(|(start, end)| ((*start).max(low), (*end).min(high)))
        .collect()
}

#[test]
fn construct_exemplars_compile_as_recorded() {
    let document = fixture("constructs.json");
    let constructs = list(&document, "constructs");
    for construct in constructs {
        let id = text_of(construct, "id");
        let exemplar = text_of(construct, "exemplar");
        let recorded = construct["rust_compiles"].as_bool();
        assert!(recorded.is_some(), "{id}: rust_compiles is not a boolean");
        assert_eq!(
            Regex::new(exemplar).is_ok(),
            recorded.unwrap_or_default(),
            "{id}: {exemplar:?} compiles on Rust differently from rust_compiles"
        );
    }
    for required in REQUIRED_CONSTRUCT_IDS {
        assert!(
            constructs
                .iter()
                .any(|construct| construct["id"] == *required),
            "required construct id {required} is missing"
        );
    }
}

#[test]
fn construct_translations_match_on_rust() {
    let document = fixture("constructs.json");
    for construct in list(&document, "constructs") {
        let id = text_of(construct, "id");
        let exemplar = text_of(construct, "exemplar");
        let translated = construct
            .get("rust_text")
            .and_then(Value::as_str)
            .map(|text| (text, Regex::new(text)));
        if let Some((text, compiled)) = &translated {
            assert!(
                compiled.is_ok(),
                "{id}: rust_text {text:?} does not compile"
            );
        }
        let as_written = Regex::new(exemplar).ok();
        for probe in construct
            .get("probes")
            .and_then(Value::as_array)
            .map_or(&[][..], Vec::as_slice)
        {
            let input = text_of(probe, "input");
            if let Some(expected) = probe.get("rust_translated").and_then(Value::as_bool) {
                let regex = translated
                    .as_ref()
                    .and_then(|(_, compiled)| compiled.as_ref().ok());
                assert!(regex.is_some(), "{id}: rust_translated without a rust_text");
                if let Some(regex) = regex {
                    assert_eq!(
                        rust_truth(regex, input),
                        expected,
                        "{id}: rust_text on {input:?}"
                    );
                }
            }
            if let Some(expected) = probe.get("rust_as_written").and_then(Value::as_bool) {
                assert!(
                    as_written.is_some(),
                    "{id}: rust_as_written for an exemplar Rust rejects"
                );
                if let Some(regex) = &as_written {
                    assert_eq!(
                        rust_truth(regex, input),
                        expected,
                        "{id}: exemplar on {input:?}"
                    );
                }
            }
        }
    }
}

#[test]
fn domain_members_match_on_rust() {
    let document = fixture("constructs.json");
    for row in list(&document, "domain") {
        let id = text_of(row, "id");
        let exemplar = text_of(row, "exemplar");
        let range = row["range"].as_array().map_or(&[][..], Vec::as_slice);
        assert_eq!(range.len(), 2, "{id}: range is [low, high]");
        let low = range.first().and_then(Value::as_u64).unwrap_or_default();
        let high = range.get(1).and_then(Value::as_u64).unwrap_or_default();
        let measured = atom_ranges(exemplar);
        assert!(
            measured.is_ok(),
            "{id}: {exemplar:?} does not compile: {measured:?}"
        );
        let cut = cut_to(
            &measured.unwrap_or_default(),
            u32::try_from(low).unwrap_or_default(),
            u32::try_from(high).unwrap_or_default(),
        );
        assert_eq!(
            ranges_value(&cut),
            row["rust_members"],
            "{id}: Rust membership"
        );
    }
    for row in list(&document, "boundary") {
        let id = text_of(row, "id");
        let atom = text_of(row, "atom");
        let regex = Regex::new(atom);
        assert!(regex.is_ok(), "{id}: {atom:?} does not compile");
        let Ok(regex) = regex else { continue };
        let offsets = row["offsets"].as_object();
        assert!(offsets.is_some(), "{id}: offsets is not an object");
        for (subject, engines) in offsets.into_iter().flatten() {
            assert_eq!(
                json!(boundary_offsets(&regex, subject)),
                engines["rust"],
                "{id}: Rust offsets on {subject:?}"
            );
        }
    }
    for row in list(&document, "iteration") {
        let id = text_of(row, "id");
        let regex = Regex::new(text_of(row, "pattern"));
        assert!(regex.is_ok(), "{id}: the pattern does not compile");
        let Ok(regex) = regex else { continue };
        let subject = text_of(row, "subject");
        let spans = sub_matches(&regex, subject)
            .into_iter()
            .map(|found| found["span"].clone())
            .collect::<Vec<_>>();
        assert_eq!(Value::Array(spans), row["rust"], "{id}: Rust match spans");
        let replaced = regex
            .replace_all(subject, NoExpand(text_of(row, "replacement")))
            .into_owned();
        assert_eq!(
            json!(replaced),
            row["rust_replaced"],
            "{id}: Rust replaced text"
        );
    }
}

fn expectations(case: &Value) -> &[Value] {
    case.get("expect")
        .and_then(Value::as_array)
        .map_or(&[], Vec::as_slice)
}

/// A span observable for SPAN-ALLOWLIST, a truth for every other consumer.
fn observable(regex: &Regex, consumer: &str, input: &str) -> Value {
    if consumer == "SPAN-ALLOWLIST" {
        regex
            .find(input)
            .map_or(Value::Null, |found| span(input, found.start(), found.end()))
    } else {
        json!(regex.is_match(input))
    }
}

fn fancy_observable(fancy: &fancy_regex::Regex, consumer: &str, input: &str) -> Value {
    if consumer == "SPAN-ALLOWLIST" {
        match fancy.find(input) {
            Ok(Some(found)) => span(input, found.start(), found.end()),
            Ok(None) => Value::Null,
            Err(error) => json!(format!("fancy error: {error}")),
        }
    } else {
        match fancy.is_match(input) {
            Ok(matched) => json!(matched),
            Err(error) => json!(format!("fancy error: {error}")),
        }
    }
}

#[test]
fn shape_family_rewrites_hold_on_rust() {
    let document = fixture("shape_families.json");
    for case in list(&document, "cases") {
        if case.get("verdict").is_some() {
            continue;
        }
        let id = text_of(case, "id");
        let consumer = text_of(case, "consumer");
        let rust_text = text_of(case, "rust_text");
        let compiled = compile_for(rust_text, consumer);
        assert!(
            compiled.is_ok(),
            "{id}: rust_text {rust_text:?} does not compile"
        );
        let Ok(regex) = compiled else { continue };
        if case["reference"] == "fancy" {
            let original = text_of(case, "original");
            let fancy = fancy_regex::Regex::new(&fancy_text(original, consumer));
            assert!(fancy.is_ok(), "{id}: fancy-regex rejects {original:?}");
            let Ok(fancy) = fancy else { continue };
            let max_len = case["max_len"].as_u64().unwrap_or_default();
            for input in exhaustive_inputs(
                text_of(case, "alphabet"),
                usize::try_from(max_len).unwrap_or_default(),
            ) {
                assert_eq!(
                    fancy_observable(&fancy, consumer, &input),
                    observable(&regex, consumer, &input),
                    "{id}: fancy on the original and regex on rust_text differ at {input:?}"
                );
            }
        }
        for expectation in expectations(case) {
            let input = text_of(expectation, "input");
            assert_eq!(
                observable(&regex, consumer, input),
                expectation["observable"],
                "{id}: rust_text on {input:?}"
            );
        }
    }
}

#[test]
fn shape_family_counterexamples_disagree_on_rust() {
    let document = fixture("shape_families.json");
    for counterexample in list(&document, "counterexamples") {
        let on_rust = counterexample["engines"]
            .as_array()
            .is_some_and(|engines| engines.iter().any(|engine| engine == "rust"));
        if !on_rust {
            continue;
        }
        let consumer = text_of(counterexample, "consumer");
        let original = text_of(counterexample, "original");
        let wrong = end_anchor_to_rust(text_of(counterexample, "wrong"));
        let witness = text_of(counterexample, "witness");
        let fancy = fancy_regex::Regex::new(&fancy_text(original, consumer));
        assert!(fancy.is_ok(), "fancy-regex rejects {original:?}");
        let regex = compile_for(&wrong, consumer);
        assert!(regex.is_ok(), "regex rejects the wrong form {wrong:?}");
        if let (Ok(fancy), Ok(regex)) = (fancy, regex) {
            assert_ne!(
                fancy_observable(&fancy, consumer, witness),
                observable(&regex, consumer, witness),
                "{original:?} and its wrong form {wrong:?} agree at {witness:?}"
            );
        }
    }
}

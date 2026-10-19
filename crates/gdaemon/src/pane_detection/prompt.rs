//! Prompt detectors, as `gobby.agents.prompt_detector` (#24382, plan 2.13.1).
//!
//! Stateless: each detector reads one bound manifest. Per-run dismissal state lives in
//! `RunDetectState`.

use std::collections::BTreeMap;
use std::sync::OnceLock;

use fancy_regex::Regex;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use super::digits::decimal_value;
use super::matcher::CompiledManifest;
use super::regions::{py_isspace, py_splitlines, py_strip};

const SELECTION_MARKERS: &str = "\u{276f}\u{203a}\u{25b6}\u{25cf}\u{2022}";
const BOX_CHARS: &[char] = &[
    ' ', '\u{2502}', '\u{256d}', '\u{256e}', '\u{2570}', '\u{256f}', '\u{2500}',
];
const TRUST_OPTION_SCAN_LINES: usize = 40;
const MAX_SELECTION_OPTIONS: usize = 8;
const MAX_OPTION_LABEL_CHARS: usize = 100;
const PROMPT_EXCERPT_LINES: usize = 12;
const PROMPT_EXCERPT_CHARS: usize = 4096;
const DOWN_KEY: &str = "down";
const UP_KEY: &str = "up";
const ENTER_KEY: &str = "enter";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PromptKind {
    Approval,
    Trust,
    Question,
    Stall,
}

impl PromptKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Approval => "approval",
            Self::Trust => "trust",
            Self::Question => "question",
            Self::Stall => "stall",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DetectedPrompt {
    pub kind: PromptKind,
    pub excerpt: String,
    pub options: Vec<(u32, String)>,
    pub fingerprint: String,
}

impl DetectedPrompt {
    pub fn to_payload(&self) -> Value {
        let options: Vec<Value> = self
            .options
            .iter()
            .map(|(option, label)| json!({"option": option, "label": label}))
            .collect();
        json!({
            "kind": self.kind.as_str(),
            "excerpt": self.excerpt,
            "options": options,
            "fingerprint": self.fingerprint,
        })
    }
}

#[derive(Debug, Clone, Copy)]
pub struct PromptDetector<'a> {
    manifest: Option<&'a CompiledManifest>,
}

impl<'a> PromptDetector<'a> {
    pub fn new(manifest: Option<&'a CompiledManifest>) -> Self {
        Self { manifest }
    }

    pub fn detect_trust_prompt(&self, pane: &str) -> bool {
        self.matches("trust_prompt", pane)
    }

    pub fn trust_dismiss_keys(&self, pane: &str) -> Vec<&'static str> {
        let (labels, selected) = selection_options(pane);
        let Some(selected) = selected else {
            return vec![ENTER_KEY];
        };
        let Some(target) = affirmative_option(&labels) else {
            return vec![ENTER_KEY];
        };
        if target == selected {
            return vec![ENTER_KEY];
        }
        let step = if target > selected { DOWN_KEY } else { UP_KEY };
        let mut keys = vec![step; target.abs_diff(selected)];
        keys.push(ENTER_KEY);
        keys
    }

    pub fn detect_loop_prompt(&self, pane: &str) -> bool {
        self.matches("loop_prompt", pane)
    }

    pub fn detect_approval_prompt(&self, pane: &str) -> bool {
        self.matches("approval_prompt", pane) || self.matches("artifact_approval", pane)
    }

    pub fn detect_queued_message_prompt(&self, pane: &str) -> bool {
        self.matches("queued_message", pane)
    }

    pub fn detect_queued_continuation_prompt(&self, pane: &str) -> bool {
        self.matches("queued_continuation", pane)
    }

    pub fn detect_prompt(&self, pane: &str) -> Option<DetectedPrompt> {
        if self.detect_approval_prompt(pane) {
            return Some(prompt_payload(pane, PromptKind::Approval));
        }
        if self.detect_trust_prompt(pane) {
            return Some(prompt_payload(pane, PromptKind::Trust));
        }
        if enumerated_options(&prompt_excerpt(pane)).len() >= 2 {
            return Some(prompt_payload(pane, PromptKind::Question));
        }
        None
    }

    fn matches(&self, rule_id: &str, pane: &str) -> bool {
        if pane.is_empty() {
            return false;
        }
        self.manifest
            .is_some_and(|manifest| manifest.match_rule(rule_id, pane).matched.is_some())
    }
}

struct Patterns {
    ansi_escape: Regex,
    decline_trust: Regex,
    parent_trust: Regex,
    affirmative_trust: Regex,
    enumerated_option: Regex,
}

fn patterns() -> &'static Patterns {
    static PATTERNS: OnceLock<Patterns> = OnceLock::new();
    PATTERNS.get_or_init(|| Patterns {
        ansi_escape: compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])"),
        decline_trust: compile(r"(?i)\b(?:no|exit|quit|cancel|don'?t|never)\b"),
        parent_trust: compile(r"(?i)\bparent\b"),
        affirmative_trust: compile(r"(?i)\b(?:yes|trust|proceed|accept)\b"),
        enumerated_option: compile(
            r"(?<!\d)(?P<option>[1-9]\d{0,2})[.)][\s\x1c-\x1f]+(?P<label>.+?)(?=(?:[\s\x1c-\x1f]*/[\s\x1c-\x1f]*|[\s\x1c-\x1f]{2,})(?:[>›❯*•-][\s\x1c-\x1f]*)?[1-9]\d{0,2}[.)][\s\x1c-\x1f]+|$)",
        ),
    })
}

fn compile(pattern: &str) -> Regex {
    Regex::new(pattern).expect("static prompt pattern compiles")
}

/// Removes terminal colour and cursor escapes, as the Python `_ANSI_ESCAPE_RE.sub`.
pub(super) fn strip_ansi(text: &str) -> String {
    patterns().ansi_escape.replace_all(text, "").into_owned()
}

fn strip_box(value: &str) -> &str {
    value.trim_matches(BOX_CHARS)
}

fn is_selected_option(line: &str) -> bool {
    strip_box(line)
        .chars()
        .next()
        .is_some_and(|first| SELECTION_MARKERS.contains(first))
}

fn option_label(line: &str) -> String {
    let label = strip_box(line);
    let label = match label.chars().next() {
        Some(first) if SELECTION_MARKERS.contains(first) => &label[first.len_utf8()..],
        _ => label,
    };
    py_strip(label).to_owned()
}

/// The marked selection block's labels and the selected row index, as `_selection_options`.
fn selection_options(pane: &str) -> (Vec<String>, Option<usize>) {
    let raw = py_splitlines(pane);
    let tail = raw.len().saturating_sub(TRUST_OPTION_SCAN_LINES);
    let lines: Vec<String> = raw[tail..].iter().map(|line| strip_ansi(line)).collect();
    let Some(marked) = lines.iter().position(|line| is_selected_option(line)) else {
        return (Vec::new(), None);
    };
    let mut start = marked;
    while start > 0 && !py_strip(&lines[start - 1]).is_empty() {
        start -= 1;
    }
    let mut end = marked;
    while end + 1 < lines.len() && !py_strip(&lines[end + 1]).is_empty() {
        end += 1;
    }
    let labels: Vec<String> = lines[start..=end]
        .iter()
        .map(|line| option_label(line))
        .collect();
    if labels.len() > MAX_SELECTION_OPTIONS {
        return (Vec::new(), None);
    }
    if labels
        .iter()
        .any(|label| label.is_empty() || label.chars().count() > MAX_OPTION_LABEL_CHARS)
    {
        return (Vec::new(), None);
    }
    (labels, Some(marked - start))
}

/// The index of the row that grants trust, or `None` when ambiguous.
fn affirmative_option(labels: &[String]) -> Option<usize> {
    let patterns = patterns();
    labels.iter().position(|label| {
        !patterns.decline_trust.is_match(label).unwrap_or(false)
            && !patterns.parent_trust.is_match(label).unwrap_or(false)
            && patterns.affirmative_trust.is_match(label).unwrap_or(false)
    })
}

fn prompt_payload(pane: &str, kind: PromptKind) -> DetectedPrompt {
    let excerpt = prompt_excerpt(pane);
    let options = enumerated_options(&excerpt);
    DetectedPrompt {
        kind,
        excerpt,
        options,
        fingerprint: pane_fingerprint(pane),
    }
}

fn prompt_excerpt(pane: &str) -> String {
    let lines = py_splitlines(pane);
    let tail = lines.len().saturating_sub(PROMPT_EXCERPT_LINES);
    let excerpt = py_strip(&lines[tail..].join("\n")).to_owned();
    let chars = excerpt.chars().count();
    if chars > PROMPT_EXCERPT_CHARS {
        return excerpt.chars().skip(chars - PROMPT_EXCERPT_CHARS).collect();
    }
    excerpt
}

/// Numbered options in the pane, first label per number, sorted by number, as
/// `_enumerated_options`. Option digits are read as Python `int()` reads them.
fn enumerated_options(pane: &str) -> Vec<(u32, String)> {
    let mut options: BTreeMap<u32, String> = BTreeMap::new();
    for raw_line in py_splitlines(pane) {
        let line = strip_box(raw_line);
        for captures in patterns().enumerated_option.captures_iter(line).flatten() {
            let Some(option) = captures
                .name("option")
                .and_then(|digits| parse_decimal(digits.as_str()))
            else {
                continue;
            };
            let label = captures
                .name("label")
                .map_or("", |label| label.as_str())
                .trim_matches(|c: char| c == ' ' || c == '\u{2502}');
            if !label.is_empty() {
                options.entry(option).or_insert_with(|| label.to_owned());
            }
        }
    }
    options.into_iter().collect()
}

fn parse_decimal(digits: &str) -> Option<u32> {
    digits
        .chars()
        .try_fold(0u32, |value, c| Some(value * 10 + decimal_value(c)?))
}

/// The `_pane_fingerprint` digest: the last twelve non-blank lines, lowercased, with runs of
/// whitespace collapsed to one space.
pub(super) fn pane_fingerprint(pane: &str) -> String {
    let lines: Vec<&str> = py_splitlines(pane)
        .into_iter()
        .map(py_strip)
        .filter(|line| !line.is_empty())
        .collect();
    let start = lines.len().saturating_sub(12);
    let normalized = lines[start..].join(" ").to_lowercase();
    let collapsed = normalized
        .split(py_isspace)
        .filter(|token| !token.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    sha256_hex(collapsed.as_bytes())
}

/// A payload for a classification not tied to pane chrome, as `classification_payload`.
pub(super) fn classification_payload(kind: PromptKind, label: &str) -> DetectedPrompt {
    let excerpt: String = label
        .split(py_isspace)
        .filter(|token| !token.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(PROMPT_EXCERPT_CHARS)
        .collect();
    let fingerprint = sha256_hex(format!("{}\0{excerpt}", kind.as_str()).as_bytes());
    DetectedPrompt {
        kind,
        excerpt,
        options: Vec::new(),
        fingerprint,
    }
}

pub(super) fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

//! Idle classification, as `gobby.agents.idle_detector` (#24382, plan 2.13.2). Every reader is
//! pure over one pane snapshot. The idle map, reprompt counters and composer ledger stay with
//! the hub.

use std::sync::OnceLock;

use fancy_regex::Regex;

use super::matcher::CompiledManifest;
use super::prompt::sha256_hex;
use super::regions::{cached, composer_region, py_isspace, py_lstrip, py_splitlines, py_strip};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ComposerState {
    Empty,
    Draft,
    Unknown,
}

impl ComposerState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Empty => "empty",
            Self::Draft => "draft",
            Self::Unknown => "unknown",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ComposerRead {
    pub state: ComposerState,
    pub line: Option<String>,
}

#[derive(Debug, Clone, Copy)]
pub struct IdleDetector<'a> {
    manifest: Option<&'a CompiledManifest>,
}

impl<'a> IdleDetector<'a> {
    pub fn new(manifest: Option<&'a CompiledManifest>) -> Self {
        Self { manifest }
    }

    /// Classifies the composer frame at the bottom of `pane`. A draft carries all text inside
    /// the frame, wrapped rows included. Anything short of a positive read is `unknown`.
    pub fn composer_read(&self, pane: Option<&str>) -> ComposerRead {
        let unknown = ComposerRead {
            state: ComposerState::Unknown,
            line: None,
        };
        let (Some(pane), Some(manifest)) = (pane, self.manifest) else {
            return unknown;
        };
        let pane = composer_text(pane);
        if hit(manifest, "composer_draft", &pane) {
            return draft_read(&pane);
        }
        if hit(manifest, "composer_empty", &pane) {
            return ComposerRead {
                state: ComposerState::Empty,
                line: None,
            };
        }
        unknown
    }

    /// Fingerprints the normalized draft lines typed at a provider prompt. `None` when no draft
    /// is visible or a queued message or continuation is already pending.
    pub fn unsubmitted_input_fingerprint(&self, pane: &str) -> Option<String> {
        let manifest = self.manifest?;
        if hit(manifest, "queued_continuation", pane) || hit(manifest, "queued_message", pane) {
            return None;
        }
        let draft_lines: Vec<String> = py_splitlines(pane)
            .into_iter()
            .filter_map(|line| {
                let stripped = py_strip(line);
                hit(manifest, "stalled_input", stripped).then(|| join_words(stripped))
            })
            .collect();
        if draft_lines.is_empty() {
            return None;
        }
        Some(sha256_hex(draft_lines.join("\n").as_bytes()))
    }

    /// Fingerprints the provider's live turn indicator. The fingerprint follows the elapsed
    /// counter, so a live turn keeps changing it while a frozen CLI does not.
    pub fn turn_in_flight_fingerprint(&self, pane: &str) -> Option<String> {
        let manifest = self.manifest?;
        let plain = plain_text(pane);
        let lines: Vec<String> = py_splitlines(&plain)
            .into_iter()
            .filter(|line| hit(manifest, "turn_in_flight", py_strip(line)))
            .map(join_words)
            .collect();
        if lines.is_empty() {
            return None;
        }
        Some(sha256_hex(lines.join("\n").as_bytes()))
    }

    /// Classifies pane output as `idle`, `context_full` or `active`, or `unknown` without a
    /// manifest.
    pub fn detect(&self, pane: &str) -> &'static str {
        let Some(manifest) = self.manifest else {
            return "unknown";
        };
        let lines = py_splitlines(py_strip(pane));
        if lines.is_empty() {
            return "active";
        }
        if hit(manifest, "context_full", pane) {
            return "context_full";
        }
        if hit(manifest, "stop_hook_blocked", pane) {
            return "idle";
        }
        if hit(manifest, "queued_message", pane) && hit(manifest, "active_work", pane) {
            return "active";
        }
        // A visible composer frame is the idle signal whatever the provider draws below it.
        // Status bars are operator-configurable, so the bottom-line walk cannot rely on them.
        if self.composer_read(Some(pane)).state != ComposerState::Unknown {
            return "idle";
        }
        for line in lines.iter().rev() {
            let stripped = py_strip(line);
            if stripped.is_empty() || hit(manifest, "status_bar", stripped) {
                continue;
            }
            let prompt_line = hit(manifest, "idle_prompt", stripped);
            if prompt_line || hit(manifest, "stalled_input", stripped) {
                return "idle";
            }
            break;
        }
        "active"
    }
}

/// Whether `rule_id` matches `text` under `manifest`.
fn hit(manifest: &CompiledManifest, rule_id: &str, text: &str) -> bool {
    manifest.match_rule(rule_id, text).matched.is_some()
}

/// Python's `" ".join(text.split())`: each run of whitespace becomes one space.
fn join_words(text: &str) -> String {
    text.split(py_isspace)
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

/// Reads the draft a framed composer holds, joining the wrapped rows under its first text row.
fn draft_read(pane: &str) -> ComposerRead {
    let frame = composer_region(pane);
    let rows = py_splitlines(&frame);
    for (index, row) in rows.iter().enumerate() {
        let Some(text) = composer_row_text(row).filter(|text| !text.is_empty()) else {
            continue;
        };
        let mut draft = vec![text];
        for continuation in &rows[index + 1..] {
            if matches!(
                py_lstrip(continuation).chars().next(),
                Some('\u{2570}' | '\u{2514}')
            ) {
                break;
            }
            let trimmed = py_strip(continuation);
            let inner = trimmed.strip_prefix('\u{2502}').unwrap_or(trimmed);
            let inner = inner.strip_suffix('\u{2502}').unwrap_or(inner);
            draft.push(py_strip(inner).to_owned());
        }
        let line = draft.join("\n");
        return ComposerRead {
            state: ComposerState::Draft,
            line: Some(line.trim_end_matches(py_isspace).to_owned()),
        };
    }
    ComposerRead {
        state: ComposerState::Draft,
        line: None,
    }
}

/// The text group of one composer row, as `_COMPOSER_ROW_RE` reads it.
fn composer_row_text(row: &str) -> Option<String> {
    let captures = composer_row().captures(row).ok().flatten()?;
    captures.name("text").map(|found| found.as_str().to_owned())
}

/// The snapshot with faint composer text blanked, as Python's `composer_text`. Claude Code's
/// prompt suggestion and the Codex and Droid placeholders are drawn faint in an empty composer,
/// while typed text never is. A reverse cell followed by faint text is blanked with it.
fn composer_text(snapshot: &str) -> String {
    if !snapshot.contains('\u{1b}') {
        return snapshot.to_owned();
    }
    let snapshot = snapshot.replace("\r\n", "\n");
    let unstyled = plain_text(&snapshot);
    let frame = composer_region(&unstyled);
    let (frame_start, frame_end) = if frame.is_empty() {
        (0, 0)
    } else {
        let prefix = match unstyled.rfind(frame.as_str()) {
            Some(index) => &unstyled[..index],
            // Python's `rfind` returns -1 for a missing frame, and `[:-1]` drops one character.
            None => {
                let mut chars = unstyled.chars();
                chars.next_back();
                chars.as_str()
            }
        };
        let start = prefix.matches('\n').count();
        (start, start + frame.matches('\n').count())
    };
    let mut faint = false;
    let mut reverse = false;
    let mut rendered = Vec::new();
    for (row_index, line) in snapshot.split('\n').enumerate() {
        let mut cells: Vec<(char, bool, bool)> = Vec::new();
        let mut position = 0;
        for escape in escapes(line) {
            cells.extend(
                line[position..escape.start]
                    .chars()
                    .map(|ch| (ch, faint, reverse)),
            );
            position = escape.end;
            let is_sgr = escape.final_byte == Some("m");
            if let Some(params) = escape
                .params
                .filter(|params| is_sgr && is_sgr_params(params))
            {
                (faint, reverse) = apply_sgr(params, faint, reverse);
            }
        }
        cells.extend(line[position..].chars().map(|ch| (ch, faint, reverse)));
        let blank_row = frame.is_empty() || (frame_start <= row_index && row_index <= frame_end);
        let row: String = cells
            .iter()
            .enumerate()
            .map(|(index, &(ch, dim, inverse))| {
                let next_faint = cells.get(index + 1).is_some_and(|next| next.1);
                if blank_row && (dim || (inverse && next_faint)) {
                    ' '
                } else {
                    ch
                }
            })
            .collect();
        rendered.push(row);
    }
    rendered.join("\n")
}

/// One escape sequence in a row: its byte span, its parameter bytes and its final byte.
struct Escape<'t> {
    start: usize,
    end: usize,
    params: Option<&'t str>,
    final_byte: Option<&'t str>,
}

fn escapes(text: &str) -> Vec<Escape<'_>> {
    escape_re()
        .captures_iter(text)
        .filter_map(Result::ok)
        .filter_map(|captures| {
            let whole = captures.get(0)?;
            Some(Escape {
                start: whole.start(),
                end: whole.end(),
                params: captures.name("params").map(|found| found.as_str()),
                final_byte: captures.name("final").map(|found| found.as_str()),
            })
        })
        .collect()
}

/// The snapshot with every escape sequence removed, as Python's `plain_text`.
fn plain_text(snapshot: &str) -> String {
    let mut plain = String::with_capacity(snapshot.len());
    let mut position = 0;
    for escape in escapes(snapshot) {
        plain.push_str(&snapshot[position..escape.start]);
        position = escape.end;
    }
    plain.push_str(&snapshot[position..]);
    plain
}

fn is_sgr_params(params: &str) -> bool {
    params
        .chars()
        .all(|ch| ch.is_ascii_digit() || ch == ';' || ch == ':')
}

/// Applies one SGR sequence to the faint and reverse attributes, as `_apply_sgr`. Colour
/// arguments (`38;5;n`, `38;2;r;g;b`) are separate parameters, not attributes.
fn apply_sgr(params: &str, mut faint: bool, mut reverse: bool) -> (bool, bool) {
    let codes: Vec<&str> = params.split(';').collect();
    let mut index = 0;
    while index < codes.len() {
        let head = codes[index].split(':').next().unwrap_or_default();
        let code = if head.is_empty() {
            0
        } else {
            head.parse::<u64>().unwrap_or(u64::MAX)
        };
        if matches!(code, 38 | 48 | 58) && !codes[index].contains(':') {
            index += match codes.get(index + 1).copied() {
                Some("5") => 3,
                Some("2") => 5,
                _ => 1,
            };
            continue;
        }
        match code {
            0 => {
                faint = false;
                reverse = false;
            }
            2 | 22 => faint = code == 2,
            7 | 27 => reverse = code == 7,
            _ => {}
        }
        index += 1;
    }
    (faint, reverse)
}

fn composer_row() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    cached(
        &RE,
        r"^[\s\x1c-\x1f]*│?[\s\x1c-\x1f]*[❯›>$][\s\x1c-\x1f]*(?P<text>.*?)[\s\x1c-\x1f]*│?[\s\x1c-\x1f]*$",
    )
}

/// `_ESCAPE_RE`: a CSI sequence (SGR when it ends in `m`), an OSC string, or a two-byte escape.
fn escape_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    cached(
        &RE,
        r"\x1b(?:\[(?P<params>[0-?]*)[ -/]*(?P<final>[@-~])|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])",
    )
}

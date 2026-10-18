//! Pane region selection (#24381, plan 2.12.2), as `_select_region`, `composer_region`,
//! `_bare_codex_composer` and `_last_prompt_box` in `gobby.agents.detection.matcher`.
//!
//! Python's `str.splitlines`, `str.strip` and `\s` count separators and whitespace that Rust's
//! `str::lines`, `char::is_whitespace` and `\s` leave out, so the helpers here follow Python.

use std::borrow::Cow;
use std::sync::OnceLock;

use fancy_regex::Regex;

use super::manifest::Region;

/// Python's `str.isspace` for one character: Unicode whitespace, plus the separators
/// U+001C to U+001F that `char::is_whitespace` does not count.
pub(super) fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

pub(super) fn py_strip(value: &str) -> &str {
    value.trim_matches(py_isspace)
}

fn py_lstrip(value: &str) -> &str {
    value.trim_start_matches(py_isspace)
}

/// Python's `str.casefold`, approximated by `to_lowercase`. The two differ on some characters,
/// such as the German sharp s; the corpus avoids them.
pub(super) fn casefold(value: &str) -> String {
    value.to_lowercase()
}

fn is_line_boundary(c: char) -> bool {
    matches!(
        c,
        '\n' | '\r'
            | '\u{b}'
            | '\u{c}'
            | '\u{1c}'
            | '\u{1d}'
            | '\u{1e}'
            | '\u{85}'
            | '\u{2028}'
            | '\u{2029}'
    )
}

/// Python's `str.splitlines`: a boundary ends a line, `\r\n` is one boundary, and a final
/// boundary does not start an empty line.
pub(super) fn py_splitlines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((index, c)) = chars.next() {
        if !is_line_boundary(c) {
            continue;
        }
        lines.push(&text[start..index]);
        start = index + c.len_utf8();
        if c == '\r'
            && let Some(&(next, '\n')) = chars.peek()
        {
            chars.next();
            start = next + 1;
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

/// The text one rule reads, as `_select_region`.
pub(super) fn select_region(pane: &str, region: Region) -> Cow<'_, str> {
    match region {
        Region::WholeRecent => Cow::Borrowed(pane),
        Region::PromptBox => Cow::Owned(last_prompt_box(pane)),
        Region::Composer => Cow::Owned(composer_region(pane)),
        Region::BottomLines(count) => {
            let lines: Vec<&str> = py_splitlines(pane)
                .into_iter()
                .filter(|line| !py_strip(line).is_empty())
                .collect();
            Cow::Owned(lines[lines.len().saturating_sub(count)..].join("\n"))
        }
    }
}

/// The bottom-most framed or Codex bare composer, as `composer_region`. Empty when no complete
/// frame is visible.
pub(super) fn composer_region(pane: &str) -> String {
    let lines = py_splitlines(pane);
    let codex = bare_codex_composer(&lines);
    if !codex.is_empty() {
        return codex;
    }
    let rules: Vec<usize> = lines
        .iter()
        .enumerate()
        .filter(|(_, line)| matches(composer_rule(), line))
        .map(|(index, _)| index)
        .collect();
    if rules.len() >= 2 {
        let open = rules[rules.len() - 2];
        let close = rules[rules.len() - 1];
        return lines[open + 1..close].join("\n");
    }
    last_prompt_box(pane)
}

fn bare_codex_composer(lines: &[&str]) -> String {
    let len = lines.len();
    if len < 3
        || !matches(codex_footer(), lines[len - 1])
        || !matches(codex_model(), lines[len - 2])
    {
        return String::new();
    }
    for index in (0..=len - 3).rev() {
        let line = lines[index];
        if matches(codex_prompt(), line) {
            return lines[index..len - 2]
                .join("\n")
                .trim_end_matches('\n')
                .to_owned();
        }
        // Wrapped composer rows keep the prompt's two-column indent; any other non-blank
        // line between an old prompt and the footer means there is no composer.
        if !py_strip(line).is_empty() && !line.starts_with("  ") {
            return String::new();
        }
    }
    String::new()
}

/// The last complete `╭ … ╰` box, as `_last_prompt_box`. Empty when none is visible.
pub(super) fn last_prompt_box(pane: &str) -> String {
    let lines = py_splitlines(pane);
    let start = lines.iter().rposition(|line| is_top_border(line));
    let Some(end) = lines.iter().rposition(|line| is_bottom_border(line)) else {
        return String::new();
    };
    if start.is_some_and(|start| start > end) {
        return String::new();
    }
    match (0..end).rev().find(|&index| is_top_border(lines[index])) {
        Some(top) => lines[top..=end].join("\n"),
        None => String::new(),
    }
}

fn is_top_border(line: &str) -> bool {
    py_lstrip(line).starts_with(['╭', '┌'])
}

fn is_bottom_border(line: &str) -> bool {
    py_lstrip(line).starts_with(['╰', '└'])
}

fn matches(regex: &Regex, text: &str) -> bool {
    regex.is_match(text).is_ok_and(|matched| matched)
}

fn cached<'a>(cell: &'a OnceLock<Regex>, pattern: &str) -> &'a Regex {
    cell.get_or_init(|| Regex::new(pattern).expect("constant composer pattern compiles"))
}

fn composer_rule() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    cached(
        &RE,
        r"^[\s\x1c-\x1f]*[─━]{8,}(?:[^\n]*[─━])?[\s\x1c-\x1f]*$",
    )
}

fn codex_prompt() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    cached(&RE, r"^[\s\x1c-\x1f]*›(?:[ \t].*)?$")
}

fn codex_model() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    cached(&RE, r"^[\s\x1c-\x1f]{2,}[^\s\x1c-\x1f].* · .*$")
}

fn codex_footer() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    cached(
        &RE,
        r"^[\s\x1c-\x1f]*(?:← for agents · )?\? for shortcuts(?:[\s\x1c-\x1f]{2,}⚠\x{FE0F}? \d+(?: warnings?)? · f2(?: to view)?)?[\s\x1c-\x1f]*$",
    )
}

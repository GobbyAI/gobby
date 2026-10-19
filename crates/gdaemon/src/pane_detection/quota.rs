//! Provider quota and capacity banners, as `gobby.agents.watchdog.quota` and the codex
//! capacity constant (#24382, plan 2.13.5).

use super::prompt::strip_ansi;
use super::regions::{py_splitlines, py_strip};

const USAGE_LIMIT_MESSAGE: &str = "You've hit your usage limit";
const RESET_PREFIX: &str = "try again at ";
const PANE_SCAN_LINE_LIMIT: usize = 15;
const PANE_DECORATION: &[char] = &[
    ' ', '\u{2502}', '\u{2503}', '\u{2506}', '\u{2507}', '\u{250a}', '\u{250b}',
];
const CODEX_MODEL_CAPACITY_MESSAGE: &str =
    "Selected model is at capacity. Please try a different model.";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProviderQuotaExhaustion {
    pub provider: String,
    pub reset_time: Option<String>,
}

/// Conclusive quota details for exact provider-owned pane messages. Only codex has a reader.
pub fn detect_provider_quota(pane: &str, provider_id: &str) -> Option<ProviderQuotaExhaustion> {
    let provider = py_strip(provider_id).to_lowercase();
    if provider != "codex" {
        return None;
    }
    let raw = py_splitlines(pane);
    let tail = raw.len().saturating_sub(PANE_SCAN_LINE_LIMIT);
    let lines: Vec<String> = raw[tail..].iter().map(|line| visible_line(line)).collect();
    let index = lines
        .iter()
        .position(|line| line.starts_with(USAGE_LIMIT_MESSAGE))?;
    let message = lines[index..]
        .iter()
        .filter(|part| !part.is_empty())
        .map(String::as_str)
        .collect::<Vec<_>>()
        .join(" ");
    let reset_time = message.find(RESET_PREFIX).and_then(|start| {
        let value = &message[start + RESET_PREFIX.len()..];
        let head = value.split_once('.').map_or(value, |(head, _)| head);
        let reset = py_strip(head);
        (!reset.is_empty()).then(|| reset.to_owned())
    });
    Some(ProviderQuotaExhaustion {
        provider,
        reset_time,
    })
}

/// The codex model-capacity banner, for the codex reader only.
pub fn capacity_pane_message(provider_id: &str) -> Option<&'static str> {
    (py_strip(provider_id).to_lowercase() == "codex").then_some(CODEX_MODEL_CAPACITY_MESSAGE)
}

/// One pane row with terminal controls and leading box decoration removed, as `_visible_line`.
fn visible_line(raw: &str) -> String {
    let visible: String = strip_ansi(raw)
        .chars()
        .filter(|&c| !is_terminal_control(c))
        .collect();
    py_strip(&visible)
        .trim_start_matches(PANE_DECORATION)
        .to_owned()
}

fn is_terminal_control(c: char) -> bool {
    matches!(c, '\u{00}'..='\u{08}' | '\u{0b}'..='\u{1f}' | '\u{7f}')
}

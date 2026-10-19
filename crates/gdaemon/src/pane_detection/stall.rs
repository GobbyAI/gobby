//! Stall classification, as `gobby.agents.stall_classifier` (#24382, plan 2.13.3).
//!
//! The per-run counters live in `StallRunState`, owned by the caller. The caller passes the
//! clock in as `now`, in seconds.

use super::matcher::CompiledManifest;
use super::prompt::strip_ansi;
use super::regions::{py_splitlines, py_strip};

const CONSECUTIVE_THRESHOLD: u32 = 2;
const MIN_CHECK_INTERVAL_SECONDS: f64 = 30.0;
const PANE_SCAN_LINE_LIMIT: usize = 50;
const PROVIDER_ERROR_LABEL: &str = "provider error";
const PANE_PROVIDER_ERROR_LABEL: &str = "provider error in pane output";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StallStatus {
    Healthy,
    ProviderStall,
    Unknown,
}

impl StallStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Healthy => "healthy",
            Self::ProviderStall => "provider_stall",
            Self::Unknown => "unknown",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StallClassification {
    pub status: StallStatus,
    pub reason: Option<String>,
    pub consecutive_hits: u32,
}

/// The per-run stall counters the caller owns, as `_RunState` holds them.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct StallRunState {
    pub consecutive_provider_hits: u32,
    pub last_check_at: f64,
}

#[derive(Debug, Clone, Copy)]
pub struct StallClassifier<'a> {
    manifest: Option<&'a CompiledManifest>,
}

impl<'a> StallClassifier<'a> {
    pub fn new(manifest: Option<&'a CompiledManifest>) -> Self {
        Self { manifest }
    }

    pub fn classify(
        &self,
        state: &mut StallRunState,
        now: f64,
        pane: Option<&str>,
        error: Option<&str>,
    ) -> StallClassification {
        let Some(manifest) = self.manifest else {
            state.consecutive_provider_hits = 0;
            state.last_check_at = now;
            return classification(StallStatus::Unknown, None, 0);
        };
        let has_pane = pane.is_some_and(|text| !py_strip(text).is_empty());
        let has_error = error.is_some_and(|text| !py_strip(text).is_empty());
        if !has_pane && !has_error {
            state.consecutive_provider_hits = 0;
            state.last_check_at = now;
            return classification(StallStatus::Healthy, None, 0);
        }
        let matched = match_provider_error(manifest, error.unwrap_or(""))
            .or_else(|| match_pane_provider_error(manifest, pane.unwrap_or("")));
        let Some(reason) = matched else {
            state.consecutive_provider_hits = 0;
            state.last_check_at = now;
            return classification(StallStatus::Healthy, None, 0);
        };
        // The first hit always counts. Later hits count only after the minimum interval, so
        // rapid re-checks of one error do not double-count it.
        if state.consecutive_provider_hits == 0 {
            state.consecutive_provider_hits = 1;
        } else if now - state.last_check_at >= MIN_CHECK_INTERVAL_SECONDS {
            state.consecutive_provider_hits += 1;
        }
        state.last_check_at = now;
        let hits = state.consecutive_provider_hits;
        if hits >= CONSECUTIVE_THRESHOLD {
            classification(StallStatus::ProviderStall, Some(reason.to_owned()), hits)
        } else {
            classification(
                StallStatus::Unknown,
                Some(format!("possible provider issue: {reason}")),
                hits,
            )
        }
    }
}

fn classification(
    status: StallStatus,
    reason: Option<String>,
    consecutive_hits: u32,
) -> StallClassification {
    StallClassification {
        status,
        reason,
        consecutive_hits,
    }
}

fn match_provider_error(manifest: &CompiledManifest, text: &str) -> Option<&'static str> {
    manifest
        .match_rule("provider_error", text)
        .matched
        .is_some()
        .then_some(PROVIDER_ERROR_LABEL)
}

/// Provider error evidence from live pane output: the last fifty lines, skipping source-shaped
/// rows, as `_match_pane_provider_error`.
fn match_pane_provider_error(manifest: &CompiledManifest, pane: &str) -> Option<&'static str> {
    let lines = py_splitlines(pane);
    let tail = lines.len().saturating_sub(PANE_SCAN_LINE_LIMIT);
    for raw_line in &lines[tail..] {
        let line = normalize_pane_line(raw_line);
        if line.is_empty()
            || manifest
                .match_rule("source_shaped", &line)
                .matched
                .is_some()
        {
            continue;
        }
        if manifest
            .match_rule("pane_provider_error", &line)
            .matched
            .is_some()
        {
            return Some(PANE_PROVIDER_ERROR_LABEL);
        }
    }
    None
}

/// Strips terminal controls before matching visible pane text.
fn normalize_pane_line(line: &str) -> String {
    let visible: String = strip_ansi(line)
        .chars()
        .filter(|&c| !is_control_char(c))
        .collect();
    py_strip(&visible).to_owned()
}

fn is_control_char(c: char) -> bool {
    matches!(c, '\u{00}'..='\u{08}' | '\u{0b}' | '\u{0c}' | '\u{0e}'..='\u{1f}' | '\u{7f}')
}

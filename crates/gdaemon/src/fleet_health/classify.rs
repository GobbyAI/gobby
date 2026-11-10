//! Pure classification of watcher samples into healthy, degraded or unstable.

use serde::{Deserialize, Serialize};

use super::policy::Thresholds;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum State {
    Healthy,
    Degraded,
    Unstable,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LogSeverity {
    Severe,
    Warning,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Probe {
    /// The endpoint answered; `status` is the HTTP status code.
    Responded { status: u16, elapsed_ms: u64 },
    /// No usable answer: connect refused, timed out or malformed.
    Failed { elapsed_ms: u64, reason: String },
}

/// Host signals. `None` means the signal could not be read, so it never trips a rule.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct HostSignals {
    pub load_per_core: Option<f64>,
    pub memory_free_percent: Option<f64>,
    pub fseventsd_cpu_percent: Option<f64>,
    pub syspolicyd_cpu_percent: Option<f64>,
    pub mds_stores_cpu_percent: Option<f64>,
    pub rustc_running: Option<bool>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Sample {
    pub at_unix: i64,
    pub probe: Probe,
    pub severe_lines: u32,
    pub warning_lines: u32,
    pub host: HostSignals,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Verdict {
    pub state: State,
    pub reasons: Vec<String>,
}

const SEVERE_LOG_PHRASES: &[&str] = &[
    "Database saturation boundary",
    "Native WS publish failed",
    "Native WS rebind failed",
    "Mailbox wake dispatch cancelled",
];

const WARNING_LOG_PHRASES: &[&str] = &[
    "Slow workspace snapshot",
    "Slow workspace attach",
    "Slow terminal attach",
];

/// Severity of one `daemon.log` line, or `None` when the line is not a fleet signal.
pub fn classify_log_line(line: &str) -> Option<LogSeverity> {
    if SEVERE_LOG_PHRASES
        .iter()
        .any(|phrase| line.contains(phrase))
    {
        return Some(LogSeverity::Severe);
    }
    // `config-db <lane> timed out`: the lane label is db, read or publish, and the daemon
    // appends its timing after the phrase. Matched on the two fixed anchors.
    if line.contains("config-db ") && line.contains(" timed out") {
        return Some(LogSeverity::Severe);
    }
    if WARNING_LOG_PHRASES
        .iter()
        .any(|phrase| line.contains(phrase))
    {
        return Some(LogSeverity::Warning);
    }
    // `Inline mcp_call <tool> ... timed out`, matched on its two fixed anchors.
    if line.contains("Inline mcp_call ") && line.contains(" timed out") {
        return Some(LogSeverity::Warning);
    }
    None
}

/// True when the last `count` samples exist and every one satisfies `predicate`.
fn last_consecutive(samples: &[Sample], count: usize, predicate: impl Fn(&Sample) -> bool) -> bool {
    count > 0 && samples.len() >= count && samples[samples.len() - count..].iter().all(predicate)
}

/// Classify the samples taken so far. `samples` is ordered oldest first; `now` is Unix seconds.
///
/// Probe, memory and severe-line rules read the newest data. Warning counts use the
/// rolling window. Load and fseventsd rules need their consecutive-sample streaks.
/// Unstable outranks degraded, and every tripped rule is listed in `reasons`.
pub fn classify(samples: &[Sample], thresholds: &Thresholds, now: i64) -> Verdict {
    let Some(latest) = samples.last() else {
        return Verdict {
            state: State::Unstable,
            reasons: vec!["no_samples".into()],
        };
    };

    let mut unstable = Vec::new();
    let mut degraded = Vec::new();

    match &latest.probe {
        Probe::Responded { status, elapsed_ms } => {
            if *status != 200 {
                unstable.push(format!("probe_status:{status}"));
            }
            if *elapsed_ms > thresholds.probe_unstable_ms {
                unstable.push(format!("probe_slow:{elapsed_ms}ms"));
            } else if *elapsed_ms > thresholds.probe_degraded_ms {
                degraded.push(format!("probe_slow:{elapsed_ms}ms"));
            }
        }
        Probe::Failed { reason, .. } => unstable.push(format!("probe_failed:{reason}")),
    }

    let recent_start = now - thresholds.severe_unstable_window_seconds;
    let recent_severe: u32 = samples
        .iter()
        .filter(|sample| sample.at_unix >= recent_start)
        .map(|sample| sample.severe_lines)
        .sum();
    if recent_severe > 0 {
        unstable.push(format!("severe_lines:{recent_severe} in last 2m"));
    }

    let window_start = now - thresholds.window_seconds;
    let window_severe: u32 = samples
        .iter()
        .filter(|sample| sample.at_unix >= window_start)
        .map(|sample| sample.severe_lines)
        .sum();
    if window_severe > 0 {
        degraded.push(format!("severe_lines:{window_severe} in window"));
    }
    let window_warnings: u32 = samples
        .iter()
        .filter(|sample| sample.at_unix >= window_start)
        .map(|sample| sample.warning_lines)
        .sum();
    if window_warnings >= thresholds.warning_lines_degraded {
        degraded.push(format!("warning_lines:{window_warnings} in window"));
    }

    let load_limit = thresholds.load_per_core_degraded;
    if last_consecutive(samples, thresholds.load_consecutive_samples, |sample| {
        sample
            .host
            .load_per_core
            .is_some_and(|load| load > load_limit)
    }) {
        let load = latest.host.load_per_core.unwrap_or(load_limit);
        degraded.push(format!("load_per_core:{load:.2}"));
    }

    if let Some(free) = latest.host.memory_free_percent
        && free < thresholds.memory_free_degraded_percent
    {
        degraded.push(format!("memory_free:{free:.0}%"));
    }

    let fsevents_limit = thresholds.fseventsd_cpu_degraded_percent;
    if last_consecutive(
        samples,
        thresholds.fseventsd_consecutive_samples,
        |sample| {
            sample
                .host
                .fseventsd_cpu_percent
                .is_some_and(|cpu| cpu > fsevents_limit)
                && sample.host.rustc_running == Some(false)
        },
    ) {
        let cpu = latest.host.fseventsd_cpu_percent.unwrap_or(fsevents_limit);
        degraded.push(format!("fseventsd_cpu:{cpu:.0}% no_rustc"));
    }

    let state = if !unstable.is_empty() {
        State::Unstable
    } else if !degraded.is_empty() {
        State::Degraded
    } else {
        State::Healthy
    };
    let reasons = match state {
        State::Unstable => unstable.into_iter().chain(degraded).collect(),
        State::Degraded => degraded,
        State::Healthy => Vec::new(),
    };
    Verdict { state, reasons }
}

#[cfg(test)]
#[path = "classify/tests.rs"]
mod tests;

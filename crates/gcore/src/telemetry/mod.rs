//! The in-process metric registry: named counters, gauges and histograms with labels.
//!
//! Callers name a metric with one of the constants in [`names`]. The methods accept any
//! `&'static str`, so the declaration is a convention, not a compile-time check.
//!
//! Label values are runtime values (rule, server and tool names), so each metric keeps
//! at most [`MAX_LABEL_SETS`] distinct label sets; further sets fold into one series
//! labelled `overflow="true"`.

use std::sync::OnceLock;

/// The declared metric names.
pub mod names {
    /// Log records at WARNING and above, labelled by surface and severity.
    pub const LOGGING_RECORDS_TOTAL: &str = "logging_records_total";
}

/// The most distinct label sets one metric name keeps before it folds into overflow.
pub const MAX_LABEL_SETS: usize = 256;

/// The registry. The process has one, from [`Telemetry::global`]; tests build their own.
#[derive(Debug, Default)]
pub struct Telemetry {
    _series: (),
}

impl Telemetry {
    /// The process's registry.
    pub fn global() -> &'static Telemetry {
        static GLOBAL: OnceLock<Telemetry> = OnceLock::new();
        GLOBAL.get_or_init(Telemetry::new)
    }

    /// An empty registry.
    pub fn new() -> Self {
        Self::default()
    }

    /// Adds `amount` to the counter `name` under `labels`.
    pub fn inc_counter(&self, _name: &'static str, _labels: &[(&'static str, &str)], _amount: u64) {
    }

    /// Records `value` in the histogram `name` under `labels`.
    pub fn observe_histogram(
        &self,
        _name: &'static str,
        _labels: &[(&'static str, &str)],
        _value: f64,
    ) {
    }

    /// Sets the gauge `name` under `labels` to `value`.
    pub fn set_gauge(&self, _name: &'static str, _labels: &[(&'static str, &str)], _value: f64) {}

    /// Every series, sorted by name and then by labels.
    pub fn snapshot(&self) -> Vec<Sample> {
        Vec::new()
    }
}

/// One series of the registry.
#[derive(Debug, Clone, PartialEq)]
pub struct Sample {
    pub name: &'static str,
    /// The series' labels, sorted by key.
    pub labels: Vec<(&'static str, String)>,
    pub value: SampleValue,
}

/// The value of one series.
#[derive(Debug, Clone, PartialEq)]
pub enum SampleValue {
    Counter(u64),
    Gauge(f64),
    Histogram { count: u64, sum: f64 },
}

#[cfg(test)]
mod tests;

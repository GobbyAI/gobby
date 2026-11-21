//! The in-process metric registry: named counters, gauges and histograms with labels.
//!
//! Callers name a metric with one of the constants in [`names`]. The methods accept any
//! `&'static str`, so the declaration is a convention, not a compile-time check.
//!
//! Label values are runtime values (rule, server and tool names), so each metric keeps
//! at most [`MAX_LABEL_SETS`] distinct label sets; further sets fold into one series
//! labelled `overflow="true"`.

use std::collections::BTreeMap;
use std::sync::{Mutex, OnceLock, PoisonError};

/// The declared metric names.
pub mod names {
    /// Log records at WARNING and above, labelled by surface and severity.
    pub const LOGGING_RECORDS_TOTAL: &str = "logging_records_total";
}

/// The most distinct label sets one metric name keeps before it folds into overflow.
pub const MAX_LABEL_SETS: usize = 256;

/// The registry. The process has one, from [`Telemetry::global`]; tests build their own.
///
/// A series keeps the kind of its first update; a later update of another kind to the
/// same name and labels is ignored.
#[derive(Debug, Default)]
pub struct Telemetry {
    metrics: Mutex<BTreeMap<&'static str, Metric>>,
}

/// One metric name's series: at most [`MAX_LABEL_SETS`] label sets, then overflow.
#[derive(Debug, Default)]
struct Metric {
    series: BTreeMap<Labels, SampleValue>,
    overflow: Option<SampleValue>,
}

type Labels = Vec<(&'static str, String)>;

/// The update one call applies to a series.
#[derive(Clone, Copy)]
enum Update {
    Add(u64),
    Observe(f64),
    Set(f64),
}

impl Update {
    fn first(self) -> SampleValue {
        match self {
            Update::Add(amount) => SampleValue::Counter(amount),
            Update::Observe(value) => SampleValue::Histogram {
                count: 1,
                sum: value,
            },
            Update::Set(value) => SampleValue::Gauge(value),
        }
    }

    fn apply(self, current: &mut SampleValue) {
        match (self, current) {
            (Update::Add(amount), SampleValue::Counter(total)) => {
                *total = total.saturating_add(amount);
            }
            (Update::Observe(value), SampleValue::Histogram { count, sum }) => {
                *count = count.saturating_add(1);
                *sum += value;
            }
            (Update::Set(value), SampleValue::Gauge(current)) => *current = value,
            _ => {}
        }
    }
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
    pub fn inc_counter(&self, name: &'static str, labels: &[(&'static str, &str)], amount: u64) {
        self.update(name, labels, Update::Add(amount));
    }

    /// Records `value` in the histogram `name` under `labels`.
    pub fn observe_histogram(
        &self,
        name: &'static str,
        labels: &[(&'static str, &str)],
        value: f64,
    ) {
        self.update(name, labels, Update::Observe(value));
    }

    /// Sets the gauge `name` under `labels` to `value`.
    pub fn set_gauge(&self, name: &'static str, labels: &[(&'static str, &str)], value: f64) {
        self.update(name, labels, Update::Set(value));
    }

    /// Every series, sorted by name and then by labels.
    pub fn snapshot(&self) -> Vec<Sample> {
        let metrics = self.metrics.lock().unwrap_or_else(PoisonError::into_inner);
        let mut samples: Vec<Sample> = Vec::new();
        for (name, metric) in metrics.iter() {
            let overflow = metric
                .overflow
                .iter()
                .map(|value| (overflow_labels(), value));
            let series = metric
                .series
                .iter()
                .map(|(labels, value)| (labels.clone(), value));
            samples.extend(series.chain(overflow).map(|(labels, value)| Sample {
                name,
                labels,
                value: value.clone(),
            }));
        }
        samples.sort_by(|left, right| (left.name, &left.labels).cmp(&(right.name, &right.labels)));
        samples
    }

    fn update(&self, name: &'static str, labels: &[(&'static str, &str)], update: Update) {
        let mut labels: Labels = labels
            .iter()
            .map(|(key, value)| (*key, (*value).to_owned()))
            .collect();
        labels.sort();
        let mut metrics = self.metrics.lock().unwrap_or_else(PoisonError::into_inner);
        let metric = metrics.entry(name).or_default();
        if let Some(current) = metric.series.get_mut(&labels) {
            update.apply(current);
        } else if metric.series.len() < MAX_LABEL_SETS {
            metric.series.insert(labels, update.first());
        } else {
            match metric.overflow.as_mut() {
                Some(current) => update.apply(current),
                None => metric.overflow = Some(update.first()),
            }
        }
    }
}

fn overflow_labels() -> Labels {
    vec![("overflow", "true".to_owned())]
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

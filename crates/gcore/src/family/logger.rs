//! A family's logging namespace and its metric helpers.

use crate::telemetry::Telemetry;

/// The logging target and metric label of one family.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FamilyLogger {
    family: &'static str,
}

impl FamilyLogger {
    pub(crate) fn new(family: &'static str) -> Self {
        Self { family }
    }

    /// The family's logging namespace, `gobby.<family>`.
    pub fn target(&self) -> String {
        format!("gobby.{}", self.family)
    }

    /// Adds `amount` to the counter `name`, labelled with the family's name.
    pub fn inc_counter(&self, name: &'static str, amount: u64) {
        Telemetry::global().inc_counter(name, &[("family", self.family)], amount);
    }

    /// Records `value` in the histogram `name`, labelled with the family's name.
    pub fn observe_histogram(&self, name: &'static str, value: f64) {
        Telemetry::global().observe_histogram(name, &[("family", self.family)], value);
    }
}

//! Thresholds read from `project_policy.fleet_health`, plus the compiled defaults that
//! match the bundled project policy template.

use serde::{Deserialize, Serialize};

use super::watch::SAMPLE_INTERVAL;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Thresholds {
    /// Probe slower than this makes the state degraded.
    pub probe_degraded_ms: u64,
    /// Probe slower than this, or not answering 200, makes the state unstable.
    pub probe_unstable_ms: u64,
    /// A severe log line inside this many seconds makes the state unstable.
    pub severe_unstable_window_seconds: i64,
    /// Rolling window for severe and warning line counts.
    pub window_seconds: i64,
    /// Warning lines inside the window that make the state degraded.
    pub warning_lines_degraded: u32,
    /// One-minute load per core over this on consecutive samples makes the state degraded.
    pub load_per_core_degraded: f64,
    pub load_consecutive_samples: usize,
    /// Free memory percent under this makes the state degraded.
    pub memory_free_degraded_percent: f64,
    /// fseventsd CPU over this with no rustc running on consecutive samples makes the state degraded.
    pub fseventsd_cpu_degraded_percent: f64,
    pub fseventsd_consecutive_samples: usize,
}

impl Default for Thresholds {
    fn default() -> Self {
        Self {
            probe_degraded_ms: 1_000,
            probe_unstable_ms: 3_000,
            severe_unstable_window_seconds: 120,
            window_seconds: 300,
            warning_lines_degraded: 10,
            load_per_core_degraded: 2.5,
            load_consecutive_samples: 2,
            memory_free_degraded_percent: 15.0,
            fseventsd_cpu_degraded_percent: 50.0,
            fseventsd_consecutive_samples: 3,
        }
    }
}

impl Thresholds {
    /// Parse the `fleet_health` block of a `project_policy` value. Missing keys take the
    /// defaults; unknown keys and out-of-range values are errors.
    pub fn from_fleet_health(value: &serde_json::Value) -> Result<Self, String> {
        if !value.is_object() {
            return Err("fleet_health must be an object".into());
        }
        let parsed: Self =
            serde_json::from_value(value.clone()).map_err(|error| error.to_string())?;
        parsed.validate()?;
        Ok(parsed)
    }

    fn validate(&self) -> Result<(), String> {
        if self.probe_degraded_ms >= self.probe_unstable_ms {
            return Err("probe_degraded_ms must be below probe_unstable_ms".into());
        }
        if !(self.load_per_core_degraded.is_finite() && self.load_per_core_degraded > 0.0) {
            return Err("load_per_core_degraded must be a positive number".into());
        }
        for (name, value) in [
            (
                "memory_free_degraded_percent",
                self.memory_free_degraded_percent,
            ),
            (
                "fseventsd_cpu_degraded_percent",
                self.fseventsd_cpu_degraded_percent,
            ),
        ] {
            if !(0.0..=100.0).contains(&value) {
                return Err(format!("{name} must be between 0 and 100"));
            }
        }
        for (name, value) in [
            (
                "severe_unstable_window_seconds",
                self.severe_unstable_window_seconds,
            ),
            ("window_seconds", self.window_seconds),
        ] {
            if value <= 0 {
                return Err(format!("{name} must be positive"));
            }
        }
        for (name, value) in [
            (
                "load_consecutive_samples",
                self.load_consecutive_samples as u64,
            ),
            (
                "fseventsd_consecutive_samples",
                self.fseventsd_consecutive_samples as u64,
            ),
        ] {
            if value == 0 {
                return Err(format!("{name} must be at least 1"));
            }
        }
        if self.warning_lines_degraded == 0 {
            return Err("warning_lines_degraded must be at least 1".into());
        }
        let retained = self.retained_samples();
        for (name, value) in [
            ("load_consecutive_samples", self.load_consecutive_samples),
            (
                "fseventsd_consecutive_samples",
                self.fseventsd_consecutive_samples,
            ),
        ] {
            if value > retained {
                return Err(format!(
                    "{name} must be at most {retained}, the samples the watcher retains"
                ));
            }
        }
        Ok(())
    }

    /// Samples the watcher keeps for the longest rule window at the fixed sample interval.
    /// Consecutive-sample rules above this could never fire.
    fn retained_samples(&self) -> usize {
        let keep = self.window_seconds.max(self.severe_unstable_window_seconds);
        let interval = i64::try_from(SAMPLE_INTERVAL.as_secs()).unwrap_or(i64::MAX);
        usize::try_from(keep / interval + 1).unwrap_or(usize::MAX)
    }
}

#[cfg(test)]
#[path = "policy/tests.rs"]
mod tests;

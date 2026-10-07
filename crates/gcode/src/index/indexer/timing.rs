use std::time::Instant;

/// Opt-in stderr diagnostics survive a killed index process's output capture.
pub(crate) struct IndexTimings {
    enabled: bool,
    phase: &'static str,
    started: Instant,
    phase_started: Instant,
}

impl IndexTimings {
    pub(crate) fn new(phase: &'static str) -> Self {
        let now = Instant::now();
        let timings = Self {
            enabled: std::env::var_os("GCODE_INDEX_TIMINGS").is_some_and(|value| value == "1"),
            phase,
            started: now,
            phase_started: now,
        };
        timings.emit("start");
        timings
    }

    pub(crate) fn phase(&mut self, phase: &'static str) {
        self.emit("end");
        self.phase = phase;
        self.phase_started = Instant::now();
        self.emit("start");
    }

    fn emit(&self, event: &str) {
        if self.enabled {
            eprintln!(
                "gcode_index_phase pid={} phase={} event={} elapsed_ms={} total_ms={}",
                std::process::id(),
                self.phase,
                event,
                self.phase_started.elapsed().as_millis(),
                self.started.elapsed().as_millis(),
            );
        }
    }
}

impl Drop for IndexTimings {
    fn drop(&mut self) {
        self.emit("end");
    }
}

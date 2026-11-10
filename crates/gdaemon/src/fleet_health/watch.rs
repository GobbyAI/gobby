//! The sampling loop's bookkeeping: rolling samples and the last non-healthy time.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::Context;
use tokio::time::MissedTickBehavior;

use super::classify::{LogSeverity, Sample, State, classify, classify_log_line};
use super::host::sample_host;
use super::logtail::LogTail;
use super::policy::Thresholds;
use super::policy_source::load_thresholds;
use super::probe::{dial_host, probe_health};
use super::state_file::{Ramp, SCHEMA_VERSION, StateFile, ThresholdsSource, write_atomic};

/// Seconds between samples.
pub const SAMPLE_INTERVAL: Duration = Duration::from_secs(30);
/// How often `project_policy` is re-read, so an edit takes effect within a minute.
const THRESHOLD_REFRESH: Duration = Duration::from_secs(60);
/// Bound on one probe. A slower answer is recorded as a failed probe.
const PROBE_TIMEOUT: Duration = Duration::from_secs(5);

/// Where the watcher reads and writes, and how it reaches the daemon and the policy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WatchConfig {
    pub state_file: PathBuf,
    pub daemon_log: PathBuf,
    pub daemon_host: String,
    pub daemon_port: u16,
    pub database_url: Option<String>,
    pub project_id: Option<String>,
    pub sample_interval: Duration,
}

impl WatchConfig {
    /// The standard layout: `state/fleet-health.json` under the home, `daemon.log` in
    /// `logs_dir`, and the daemon dialled at the address its bootstrap bind names.
    pub fn under_home(
        home: &Path,
        logs_dir: &Path,
        bind_host: &str,
        daemon_port: u16,
        database_url: Option<String>,
        project_id: Option<String>,
    ) -> Self {
        Self {
            state_file: home.join("state").join("fleet-health.json"),
            daemon_log: logs_dir.join("daemon.log"),
            daemon_host: dial_host(bind_host),
            daemon_port,
            database_url,
            project_id,
            sample_interval: SAMPLE_INTERVAL,
        }
    }
}

/// The thresholds in force. A failed policy read keeps the last good value.
#[derive(Debug, Default)]
pub struct ThresholdCache {
    last_good: Option<Thresholds>,
}

impl ThresholdCache {
    /// Fold one policy read into the cache and report where the thresholds came from.
    pub fn resolve(
        &mut self,
        read: Result<Option<Thresholds>, String>,
    ) -> (Thresholds, ThresholdsSource) {
        match read {
            // No enabled writer or no fleet block: the defaults are the source, and an
            // old block must not survive as the last good value.
            Ok(None) => {
                self.last_good = None;
                (Thresholds::default(), ThresholdsSource::Defaults)
            }
            Ok(Some(thresholds)) => {
                self.last_good = Some(thresholds.clone());
                (thresholds, ThresholdsSource::ProjectPolicy)
            }
            Err(_) => match &self.last_good {
                Some(last_good) => (last_good.clone(), ThresholdsSource::LastGood),
                None => (Thresholds::default(), ThresholdsSource::Defaults),
            },
        }
    }
}

/// What one sample needs between ticks: the rolling window, the thresholds in force and
/// when `project_policy` was last read.
struct Sampler {
    watcher: Watcher,
    tail: LogTail,
    cache: ThresholdCache,
    thresholds: (Thresholds, ThresholdsSource),
    refreshed_at: Option<Instant>,
}

impl Sampler {
    fn new(config: &WatchConfig) -> Self {
        Self {
            watcher: Watcher::new(),
            tail: LogTail::new(&config.daemon_log),
            cache: ThresholdCache::default(),
            thresholds: (Thresholds::default(), ThresholdsSource::Defaults),
            refreshed_at: None,
        }
    }

    /// Take one sample and rewrite the state file. A sample that fails writes nothing, so the
    /// file ages past its staleness bound and admission fails closed.
    async fn sample(&mut self, config: &WatchConfig) -> anyhow::Result<()> {
        if self
            .refreshed_at
            .is_none_or(|at| at.elapsed() >= THRESHOLD_REFRESH)
        {
            self.refreshed_at = Some(Instant::now());
            let read = match config.database_url.as_deref() {
                Some(url) => load_thresholds(url, config.project_id.as_deref()).await,
                None => Err("no database URL configured".to_string()),
            };
            if let Err(error) = &read {
                eprintln!("fleet-watch: project_policy read failed: {error}");
            }
            self.thresholds = self.cache.resolve(read);
        }

        let probe = probe_health(&config.daemon_host, config.daemon_port, PROBE_TIMEOUT).await;
        let host = sample_host().await;
        let lines = self.tail.poll().context("reading the daemon log")?;
        let (severe_lines, warning_lines) = count_log_lines(&lines);
        let sample = Sample {
            at_unix: now_unix(),
            probe,
            severe_lines,
            warning_lines,
            host,
        };
        let (policy, source) = &self.thresholds;
        let state = self
            .watcher
            .record(sample, policy, *source, std::process::id());
        write_atomic(&config.state_file, &state).context("writing the fleet state file")?;
        Ok(())
    }
}

/// Sample every `config.sample_interval` and rewrite the state file on each sample. `once`
/// takes a single sample and returns its error. In the loop a failed sample is reported and
/// skipped: it writes nothing, so the state file goes stale and admission fails closed, and
/// the next tick tries again. The watcher never exits on one bad sample.
pub async fn run(config: WatchConfig, once: bool) -> anyhow::Result<()> {
    let mut sampler = Sampler::new(&config);
    if once {
        return sampler.sample(&config).await;
    }
    let mut ticker = tokio::time::interval(config.sample_interval);
    ticker.set_missed_tick_behavior(MissedTickBehavior::Delay);
    loop {
        ticker.tick().await;
        if let Err(error) = sampler.sample(&config).await {
            eprintln!("fleet-watch: sample skipped: {error:#}");
        }
    }
}

/// Severe and warning line counts among `lines`.
fn count_log_lines(lines: &[String]) -> (u32, u32) {
    lines.iter().fold((0, 0), |(severe, warning), line| {
        match classify_log_line(line) {
            Some(LogSeverity::Severe) => (severe.saturating_add(1), warning),
            Some(LogSeverity::Warning) => (severe, warning.saturating_add(1)),
            None => (severe, warning),
        }
    })
}

fn now_unix() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| {
            i64::try_from(elapsed.as_secs()).unwrap_or(i64::MAX)
        })
}

#[derive(Debug, Default)]
pub struct Watcher {
    samples: Vec<Sample>,
    last_non_healthy_at: Option<i64>,
}

impl Watcher {
    pub fn new() -> Self {
        Self::default()
    }

    /// Samples currently inside the rolling window.
    pub fn sample_count(&self) -> usize {
        self.samples.len()
    }

    /// Add `sample`, drop samples older than the window, classify, and build the state file.
    pub fn record(
        &mut self,
        sample: Sample,
        thresholds: &Thresholds,
        source: ThresholdsSource,
        watcher_pid: u32,
    ) -> StateFile {
        let now = sample.at_unix;
        self.samples.push(sample);
        // Keep enough history for every rule: the severe-line window and the warning window.
        let keep_seconds = thresholds
            .window_seconds
            .max(thresholds.severe_unstable_window_seconds);
        let keep_from = now - keep_seconds;
        self.samples.retain(|sample| sample.at_unix >= keep_from);

        let verdict = classify(&self.samples, thresholds, now);
        if verdict.state != State::Healthy {
            self.last_non_healthy_at = Some(now);
        }
        StateFile {
            schema_version: SCHEMA_VERSION,
            state: verdict.state,
            sampled_at_unix: now,
            last_non_healthy_at_unix: self.last_non_healthy_at,
            reasons: verdict.reasons,
            ramp: Ramp {
                max_new_seats: thresholds.max_new_seats,
                ramp_window_seconds: thresholds.ramp_window_seconds,
            },
            thresholds_source: source,
            watcher_pid,
        }
    }
}

#[cfg(test)]
#[path = "watch/tests.rs"]
mod tests;

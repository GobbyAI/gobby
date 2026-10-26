//! The compare log: one JSON line per claimed request a Compare-mode family handled,
//! and the report `gdaemon compare-report` reads back from it.

use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, PoisonError};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};

/// The log's file name under `<home>/logs/`.
pub const LOG_FILE: &str = "front-door-compare.jsonl";

/// What compare concluded about one claimed request.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Match,
    Diff,
    NativeError,
    Verified,
    Mismatch,
    Skipped,
    Waived,
}

/// The parity waiver a changed behaviour reports under: its exact manifest id and
/// the decision citation that approved it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WaiverRef {
    pub id: String,
    pub decision: String,
}

/// One log line. Carries no request or response body values.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Record {
    pub ts: i64,
    pub family: String,
    pub method: String,
    pub path: String,
    pub outcome: Outcome,
    pub native_status: Option<u16>,
    pub proxied_status: Option<u16>,
    pub detail: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub waiver: Option<WaiverRef>,
}

/// Appends records to `<home>/logs/front-door-compare.jsonl`.
pub struct CompareLog {
    file: Mutex<File>,
    write_failed: AtomicBool,
}

impl CompareLog {
    pub fn open(home: &Path) -> std::io::Result<Self> {
        let path = log_path(home);
        if let Some(logs) = path.parent() {
            std::fs::create_dir_all(logs)?;
        }
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        Ok(Self {
            file: Mutex::new(file),
            write_failed: AtomicBool::new(false),
        })
    }

    /// Writes one line with a single `write_all` on the append-mode file, so lines
    /// never interleave. A failure is reported once and never reaches the request.
    pub fn append(&self, record: &Record) {
        let result = serde_json::to_vec(record)
            .map_err(std::io::Error::from)
            .and_then(|mut line| {
                line.push(b'\n');
                let mut file = self.file.lock().unwrap_or_else(PoisonError::into_inner);
                file.write_all(&line)
            });
        if let Err(error) = result
            && !self.write_failed.swap(true, Ordering::Relaxed)
        {
            eprintln!("gdaemon serve: front-door compare log write failed: {error}");
        }
    }
}

/// The log path under a Gobby home.
pub fn log_path(home: &Path) -> PathBuf {
    home.join("logs").join(LOG_FILE)
}

/// Outcome counts per family within a window.
#[derive(Debug, Default, PartialEq, Eq, Serialize)]
pub struct Report {
    pub since: Option<i64>,
    pub first_ts: Option<i64>,
    pub last_ts: Option<i64>,
    pub total: u64,
    pub families: BTreeMap<String, BTreeMap<Outcome, u64>>,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    pub waived: BTreeMap<String, WaivedCount>,
}

/// How many waived records cite one waiver id, and the decision it cites.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct WaivedCount {
    pub decision: String,
    pub count: u64,
}

impl Report {
    /// True when the window holds at least one `match` or `verified` record and none is a
    /// `diff`, `native_error`, `mismatch` or unwaived `skipped`. A waived record neither passes
    /// nor fails: it records a deliberate divergence, which is not parity evidence.
    pub fn passes(&self) -> bool {
        let mut passed = false;
        for outcome in self.families.values().flat_map(BTreeMap::keys) {
            match outcome {
                Outcome::Match | Outcome::Verified => passed = true,
                Outcome::Waived => {}
                Outcome::Diff | Outcome::NativeError | Outcome::Mismatch | Outcome::Skipped => {
                    return false;
                }
            }
        }
        passed
    }
}

/// Counts the records at `path` whose `ts` is at or after `since`. A malformed line
/// is an error.
pub fn report(path: &Path, since: Option<i64>) -> Result<Report> {
    let text =
        std::fs::read_to_string(path).with_context(|| format!("cannot read {}", path.display()))?;
    let mut report = Report {
        since,
        ..Report::default()
    };
    for (index, line) in text.lines().enumerate() {
        let record: Record = serde_json::from_str(line).with_context(|| {
            format!("{}:{}: malformed compare record", path.display(), index + 1)
        })?;
        if since.is_some_and(|since| record.ts < since) {
            continue;
        }
        report.first_ts = Some(report.first_ts.map_or(record.ts, |ts| ts.min(record.ts)));
        report.last_ts = Some(report.last_ts.map_or(record.ts, |ts| ts.max(record.ts)));
        report.total += 1;
        if record.outcome == Outcome::Waived {
            let waiver = record.waiver.as_ref().with_context(|| {
                format!(
                    "{}:{}: waived record names no waiver",
                    path.display(),
                    index + 1
                )
            })?;
            let counted = report
                .waived
                .entry(waiver.id.clone())
                .or_insert_with(|| WaivedCount {
                    decision: waiver.decision.clone(),
                    count: 0,
                });
            if counted.decision != waiver.decision {
                bail!(
                    "{}:{}: waiver {} cites two decisions",
                    path.display(),
                    index + 1,
                    waiver.id
                );
            }
            counted.count += 1;
        }
        *report
            .families
            .entry(record.family)
            .or_default()
            .entry(record.outcome)
            .or_default() += 1;
    }
    Ok(report)
}

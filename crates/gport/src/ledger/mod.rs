//! `gport ledger`: the parity ledger from the frozen rows, with ownership and dispositions.
//!
//! The contract ledger is rendered as scripts/generate_parity_ledger.py renders it. The coverage
//! and disposition reports are the lines of scripts/port/check_coverage.py and
//! scripts/port/check_dispositions.py. A run whose stdout carries data (`--stdout`, `--owners`)
//! sends its report to stderr, so stdout holds the data alone.

mod coverage;
mod dispositions;
pub(crate) mod pyfmt;
pub(crate) mod pyre;
mod render;
mod statuses;
mod waivers;

use std::collections::BTreeSet;
use std::fs::{self, File};
use std::io::Write;
use std::path::Path;

use anyhow::{Context, Result, bail};
use serde_json::Value;

use crate::rows;
use crate::{
    CONTRACT_LEDGER_PATH, DISPOSITIONS_DIR, FROZEN_LEDGER_PATH, MAP_PATH, OPEN_NODES_PATH,
    STATUS_SHARDS_DIR, WAIVERS_PATH,
};

#[derive(Debug, Clone, Copy, Default)]
pub struct Options {
    pub stdout: bool,
    pub exit_rule: bool,
    pub owners: bool,
}

/// Run the ledger verb under `root`. Returns `Ok(true)` when coverage and dispositions are clean
/// and, with `--exit-rule`, no row blocks the exit rule.
pub fn run(root: &Path, options: Options, out: &mut dyn Write) -> anyhow::Result<bool> {
    if options.owners && (options.stdout || options.exit_rule) {
        bail!(
            "--owners prints only ownership lines; it cannot be combined with --stdout or --exit-rule"
        );
    }
    let frozen = read_text(&root.join(FROZEN_LEDGER_PATH))?;
    // Ownership matches the keys as written, as scripts/port/check_coverage.py does.
    let keys: Vec<String> = rows::rows(&frozen).into_iter().map(|row| row.key).collect();
    let map = read_json(&root.join(MAP_PATH))?;
    let chunks = map
        .get("chunks")
        .and_then(Value::as_array)
        .context("the map has no chunks list")?;
    let cover = coverage::Coverage::check(chunks, &keys)?;
    let nodes = read_json(&root.join(OPEN_NODES_PATH))?;
    let files = read_dispositions(root)?;
    let disposition_report = dispositions::Dispositions::check(&nodes, &map, &files)?;

    let blocking = if options.owners {
        for line in cover.owner_lines() {
            writeln!(out, "{line}")?;
        }
        None
    } else {
        let surfaces: BTreeSet<String> = keys.iter().map(|key| rendered_surface(key)).collect();
        // The Python generator loads the waivers before the statuses; keep that order.
        let waiver_map = waivers::load(&root.join(WAIVERS_PATH), &surfaces)?;
        let status_map = statuses::load(&root.join(STATUS_SHARDS_DIR))?;
        let rendered = render::render(&surfaces, &status_map, &waiver_map)?;
        if options.stdout {
            out.write_all(rendered.text.as_bytes())?;
        } else {
            write_contract_ledger(&root.join(CONTRACT_LEDGER_PATH), &rendered.text)?;
        }
        Some(rendered.blocking)
    };

    let mut stderr = std::io::stderr().lock();
    let report: &mut dyn Write = if options.stdout || options.owners {
        &mut stderr
    } else {
        out
    };
    let mut clean = cover.clean() && disposition_report.clean();
    for line in cover.lines() {
        writeln!(report, "{line}")?;
    }
    for line in disposition_report.lines() {
        writeln!(report, "{line}")?;
    }
    if let Some(blocking) = blocking {
        writeln!(report, "EXIT_RULE blocking={blocking}")?;
        if options.exit_rule && blocking > 0 {
            clean = false;
        }
    }
    Ok(clean)
}

/// The surface a frozen row key names: the render escapes `|` back to `\|`, so the key's escape is
/// undone here. Ownership never sees this form.
fn rendered_surface(key: &str) -> String {
    key.replace("\\|", "|")
}

/// Write the contract ledger through a sibling temp file, renamed over `path` once it is synced, so
/// an interrupted write leaves the existing ledger in place.
fn write_contract_ledger(path: &Path, text: &str) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("cannot create {}", parent.display()))?;
    }
    let temp = sibling_temp(path)?;
    let written = write_synced(&temp, text).and_then(|()| {
        fs::rename(&temp, path).with_context(|| format!("cannot write {}", path.display()))
    });
    if written.is_err() {
        // Best effort: the failed write is the error to report, not a leftover temp file.
        let _ = fs::remove_file(&temp);
    }
    written
}

/// A temp path beside `path`, unique to this process.
fn sibling_temp(path: &Path) -> Result<std::path::PathBuf> {
    let name = path
        .file_name()
        .with_context(|| format!("{} has no file name", path.display()))?;
    let mut temp_name = name.to_os_string();
    temp_name.push(format!(".{}.tmp", std::process::id()));
    Ok(path.with_file_name(temp_name))
}

fn write_synced(path: &Path, text: &str) -> Result<()> {
    let mut file =
        File::create(path).with_context(|| format!("cannot write {}", path.display()))?;
    file.write_all(text.as_bytes())
        .and_then(|()| file.sync_all())
        .with_context(|| format!("cannot write {}", path.display()))
}

/// Every `*.json` disposition file, sorted by name, parsed.
fn read_dispositions(root: &Path) -> Result<Vec<(String, Value)>> {
    let dir = root.join(DISPOSITIONS_DIR);
    let entries = fs::read_dir(&dir).with_context(|| format!("cannot read {}", dir.display()))?;
    let mut names = Vec::new();
    for entry in entries {
        let entry = entry.with_context(|| format!("cannot list {}", dir.display()))?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.ends_with(".json") {
            names.push(name);
        }
    }
    names.sort();
    names
        .into_iter()
        .map(|name| -> Result<(String, Value)> {
            let value = read_json(&dir.join(&name))?;
            Ok((name, value))
        })
        .collect()
}

fn read_text(path: &Path) -> Result<String> {
    fs::read_to_string(path).with_context(|| format!("cannot read {}", path.display()))
}

fn read_json(path: &Path) -> Result<Value> {
    let text = read_text(path)?;
    serde_json::from_str(&text).with_context(|| format!("{} is not valid JSON", path.display()))
}

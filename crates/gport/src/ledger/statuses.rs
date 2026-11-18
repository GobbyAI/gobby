//! Status shards: the merged surface statuses that scripts/generate_parity_ledger.py `load_statuses`
//! reads from `docs/contracts/parity-ledger-status.d/*.json`.

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use anyhow::{Context, bail};
use serde_json::Value;

use super::pyfmt::{py_repr, py_string_repr};

/// One surface's status and its note. Notes are empty for string entries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParityStatus {
    pub status: String,
    pub note: String,
}

/// Whether `status` is `dropped by decision #N` with a positive N, the Python pattern
/// `dropped by decision #[1-9][0-9]*` matched in full.
pub fn is_dropped(status: &str) -> bool {
    let Some(number) = status.strip_prefix("dropped by decision #") else {
        return false;
    };
    let mut digits = number.chars();
    matches!(digits.next(), Some('1'..='9')) && digits.all(|c| c.is_ascii_digit())
}

/// Merge every `*.json` shard in `dir`, in filename order. A surface in two shards, an invalid
/// entry or an invalid status is an error, as in the Python loader.
pub fn load(dir: &Path) -> anyhow::Result<BTreeMap<String, ParityStatus>> {
    let shards = shard_paths(dir)?;
    if shards.is_empty() {
        bail!("No parity status shards found in {}", dir.display());
    }
    let mut statuses: BTreeMap<String, ParityStatus> = BTreeMap::new();
    let mut owners: BTreeMap<String, String> = BTreeMap::new();
    for (name, path) in shards {
        let text =
            fs::read_to_string(&path).with_context(|| format!("cannot read {}", path.display()))?;
        let data: Value =
            serde_json::from_str(&text).with_context(|| format!("{name} is not valid JSON"))?;
        let Some(object) = data.as_object() else {
            bail!("Parity status mapping must be a JSON object: {name}");
        };
        let mut entries: Vec<(&String, &Value)> = object.iter().collect();
        entries.sort_by(|left, right| left.0.cmp(right.0));
        for (surface, entry) in entries {
            if statuses.contains_key(surface) {
                let first = owners.get(surface).map_or("", String::as_str);
                bail!(
                    "Duplicate parity surface {} in {first} and {name}",
                    py_string_repr(surface)
                );
            }
            let (status_value, note) = parse_entry(surface, entry, &name)?;
            let status = match status_value.as_str() {
                Some(text) if text == "proxied" || text == "delegated" || is_dropped(text) => {
                    text.to_owned()
                }
                _ => bail!(
                    "Invalid parity status for {surface}: {}",
                    py_repr(status_value)
                ),
            };
            owners.insert(surface.clone(), name.clone());
            statuses.insert(surface.clone(), ParityStatus { status, note });
        }
    }
    Ok(statuses)
}

/// The status value and note of one entry: a bare status string, or an object with exactly
/// `status` and `note`, both strings.
fn parse_entry<'a>(
    surface: &str,
    entry: &'a Value,
    shard: &str,
) -> anyhow::Result<(&'a Value, String)> {
    let Some(object) = entry.as_object() else {
        return Ok((entry, String::new()));
    };
    let note = object.get("note").and_then(Value::as_str);
    match (object.len() == 2, object.get("status"), note) {
        (true, Some(status), Some(note)) => Ok((status, note.to_owned())),
        _ => bail!(
            "Invalid parity entry for {surface} in {shard}: \
             expected a status string or an object with string status and note"
        ),
    }
}

/// The `*.json` files in `dir`, sorted by filename. A missing directory holds no shards.
fn shard_paths(dir: &Path) -> anyhow::Result<Vec<(String, PathBuf)>> {
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => {
            return Err(anyhow::Error::new(error).context(format!("cannot read {}", dir.display())));
        }
    };
    let mut shards = Vec::new();
    for entry in entries {
        let entry = entry.with_context(|| format!("cannot list {}", dir.display()))?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.ends_with(".json") {
            shards.push((name, entry.path()));
        }
    }
    shards.sort_by(|left, right| left.0.cmp(&right.0));
    Ok(shards)
}

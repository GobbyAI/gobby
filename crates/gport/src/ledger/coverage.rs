//! Row ownership: scripts/port/check_coverage.py. Each chunk's ledger selectors, matched by the
//! shared `rows::Selectors` engine, are searched in every frozen row key; a row must be owned by
//! exactly one chunk.

use std::collections::BTreeMap;

use anyhow::{Context, Result};
use serde_json::Value;

use super::pyfmt::json_counts;
use crate::map::ledger_selectors;
use crate::rows::Selectors;

/// Most ORPHAN and DOUBLE lines printed before the rest are counted.
const LINE_CAP: usize = 60;

/// Ownership of the frozen row keys by the map's chunks.
pub struct Coverage {
    /// Every row, counting repeats, as the summary line counts them.
    rows: usize,
    /// Every row key in file order, counting repeats.
    row_keys: Vec<String>,
    /// Each distinct key's sorted owner chunk ids.
    owners: BTreeMap<String, Vec<String>>,
    /// Each distinct key in first-occurrence order.
    distinct: Vec<String>,
    /// Each row, counting repeats, that no selector owns.
    orphans: Vec<String>,
    /// One message per selector that does not compile.
    bad: Vec<String>,
}

impl Coverage {
    /// Match every selector in `chunks` against every key, as written.
    pub fn check(chunks: &[Value], keys: &[String]) -> Result<Self> {
        let pairs = selector_pairs(chunks)?;
        let selectors = Selectors::from_selectors(
            pairs
                .iter()
                .map(|(id, selector)| (id.as_str(), selector.as_str())),
        );
        let mut owners = BTreeMap::new();
        let mut distinct = Vec::new();
        for key in keys {
            if owners.contains_key(key) {
                continue;
            }
            let ids: Vec<String> = selectors
                .try_owners(key)?
                .into_iter()
                .map(str::to_owned)
                .collect();
            owners.insert(key.clone(), ids);
            distinct.push(key.clone());
        }
        let orphans = keys
            .iter()
            .filter(|key| owners.get(*key).is_some_and(Vec::is_empty))
            .cloned()
            .collect();
        Ok(Self {
            rows: keys.len(),
            row_keys: keys.to_vec(),
            owners,
            distinct,
            orphans,
            bad: selectors.bad,
        })
    }

    /// Whether every row is owned by exactly one chunk and every selector compiles.
    pub fn clean(&self) -> bool {
        self.orphans.is_empty() && self.doubles().next().is_none() && self.bad.is_empty()
    }

    /// `<row key>\t<owner ids>` for each row, one line per row including repeats, sorted by key.
    /// Ids are comma-joined, so an orphan has an empty field and a double-owned row lists each
    /// owner.
    pub fn owner_lines(&self) -> Vec<String> {
        let mut sorted: Vec<&String> = self.row_keys.iter().collect();
        sorted.sort();
        sorted
            .into_iter()
            .map(|key| format!("{key}\t{}", self.ids_of(key).join(",")))
            .collect()
    }

    /// The summary, BAD, ORPHAN, DOUBLE and PER_CHUNK lines, in the Python checker's order.
    pub fn lines(&self) -> Vec<String> {
        let mut per_chunk: BTreeMap<&str, usize> = BTreeMap::new();
        for ids in self.owners.values() {
            if let [only] = ids.as_slice() {
                *per_chunk.entry(only.as_str()).or_default() += 1;
            }
        }
        let owned_once: usize = per_chunk.values().sum();
        let doubles: Vec<(&str, &Vec<String>)> = self.doubles().collect();
        let mut lines = vec![format!(
            "rows={} owned_once={owned_once} orphans={} double_owned={} bad_selectors={}",
            self.rows,
            self.orphans.len(),
            doubles.len(),
            self.bad.len()
        )];
        lines.extend(self.bad.iter().map(|line| format!("BAD {line}")));
        lines.extend(
            self.orphans
                .iter()
                .take(LINE_CAP)
                .map(|key| format!("ORPHAN {key}")),
        );
        push_overflow(&mut lines, self.orphans.len(), "orphans");
        for (key, ids) in doubles.iter().take(LINE_CAP) {
            lines.push(format!("DOUBLE {key} -> {}", ids.join(",")));
        }
        push_overflow(&mut lines, doubles.len(), "double-owned");
        lines.push(format!(
            "PER_CHUNK {}",
            json_counts(per_chunk.iter().map(|(id, count)| (*id, *count)))
        ));
        lines
    }

    /// The owner ids of a distinct key; a key with none has an empty list.
    fn ids_of(&self, key: &str) -> &[String] {
        self.owners.get(key).map(Vec::as_slice).unwrap_or_default()
    }

    /// Distinct keys with more than one owner, in first-occurrence order.
    fn doubles(&self) -> impl Iterator<Item = (&str, &Vec<String>)> {
        self.distinct.iter().filter_map(|key| {
            self.owners
                .get(key)
                .filter(|ids| ids.len() > 1)
                .map(|ids| (key.as_str(), ids))
        })
    }
}

/// Each chunk's `(id, selector)` pairs, in map order.
fn selector_pairs(chunks: &[Value]) -> Result<Vec<(String, String)>> {
    let mut pairs = Vec::new();
    for chunk in chunks {
        let id = chunk
            .get("id")
            .and_then(Value::as_str)
            .context("a map chunk has no string id")?;
        for selector in ledger_selectors(id, chunk.get("ledger_selectors"))? {
            pairs.push((id.to_owned(), selector));
        }
    }
    Ok(pairs)
}

/// `... N more <what>` when `total` is longer than the printed cap.
fn push_overflow(lines: &mut Vec<String>, total: usize, what: &str) {
    if total > LINE_CAP {
        lines.push(format!("... {} more {what}", total - LINE_CAP));
    }
}

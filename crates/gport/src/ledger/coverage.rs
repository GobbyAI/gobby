//! Row ownership: scripts/port/check_coverage.py. Each chunk's ledger selectors are Python `re`
//! patterns searched in every frozen row key; a row must be owned by exactly one chunk.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Context, Result, bail};
use fancy_regex::Regex;
use serde_json::Value;

use super::pyfmt::{json_counts, py_string_repr};

/// Most ORPHAN and DOUBLE lines printed before the rest are counted.
const LINE_CAP: usize = 60;

/// Ownership of the frozen row keys by the map's chunks.
pub struct Coverage {
    /// Every row, counting repeats, as the summary line counts them.
    rows: usize,
    /// Each distinct key in first-occurrence order, with its sorted owner chunk ids.
    owners: Vec<(String, Vec<String>)>,
    /// Each row, counting repeats, that no selector owns.
    orphans: Vec<String>,
    /// One message per selector that does not compile.
    bad: Vec<String>,
}

impl Coverage {
    /// Match every selector in `chunks` against every key.
    pub fn check(chunks: &[Value], keys: &[String]) -> Result<Self> {
        let (selectors, bad) = compile(chunks)?;
        let mut seen: BTreeSet<&str> = BTreeSet::new();
        let mut unowned: BTreeSet<&str> = BTreeSet::new();
        let mut owners = Vec::new();
        for key in keys {
            if !seen.insert(key.as_str()) {
                continue;
            }
            let ids = owners_of(&selectors, key)?;
            if ids.is_empty() {
                unowned.insert(key.as_str());
            }
            owners.push((key.clone(), ids));
        }
        let orphans = keys
            .iter()
            .filter(|key| unowned.contains(key.as_str()))
            .cloned()
            .collect();
        Ok(Self {
            rows: keys.len(),
            owners,
            orphans,
            bad,
        })
    }

    /// Whether every row is owned by exactly one chunk and every selector compiles.
    pub fn clean(&self) -> bool {
        self.orphans.is_empty() && self.doubles().next().is_none() && self.bad.is_empty()
    }

    /// `<row key>\t<owner ids>` for each distinct key, sorted by key. Ids are comma-joined, so an
    /// orphan has an empty field and a double-owned row lists each owner.
    pub fn owner_lines(&self) -> Vec<String> {
        let mut sorted: Vec<&(String, Vec<String>)> = self.owners.iter().collect();
        sorted.sort_by(|left, right| left.0.cmp(&right.0));
        sorted
            .into_iter()
            .map(|(key, ids)| format!("{key}\t{}", ids.join(",")))
            .collect()
    }

    /// The summary, BAD, ORPHAN, DOUBLE and PER_CHUNK lines, in the Python checker's order.
    pub fn lines(&self) -> Vec<String> {
        let mut per_chunk: BTreeMap<&str, usize> = BTreeMap::new();
        for (_, ids) in &self.owners {
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

    /// Distinct keys with more than one owner, in first-occurrence order.
    fn doubles(&self) -> impl Iterator<Item = (&str, &Vec<String>)> {
        self.owners
            .iter()
            .filter(|(_, ids)| ids.len() > 1)
            .map(|(key, ids)| (key.as_str(), ids))
    }
}

/// The sorted, distinct chunk ids with a selector that matches `key`.
fn owners_of(selectors: &[Selector], key: &str) -> Result<Vec<String>> {
    let mut ids = Vec::new();
    for (chunk, pattern) in selectors {
        if pattern
            .is_match(key)
            .with_context(|| format!("selector of {chunk} failed on {key}"))?
        {
            ids.push(chunk.clone());
        }
    }
    ids.sort_unstable();
    ids.dedup();
    Ok(ids)
}

/// A selector that compiled, with the id of the chunk that holds it.
type Selector = (String, Regex);

/// Every selector that compiles, paired with its chunk id, and one message per one that does not.
fn compile(chunks: &[Value]) -> Result<(Vec<Selector>, Vec<String>)> {
    let mut compiled: Vec<Selector> = Vec::new();
    let mut bad = Vec::new();
    for chunk in chunks {
        let id = chunk
            .get("id")
            .and_then(Value::as_str)
            .context("a map chunk has no string id")?;
        for selector in selectors_of(chunk)? {
            match Regex::new(selector) {
                Ok(pattern) => compiled.push((id.to_owned(), pattern)),
                Err(error) => bad.push(format!("{id}: {}: {error}", py_string_repr(selector))),
            }
        }
    }
    Ok((compiled, bad))
}

/// A chunk's `ledger_selectors`: absent or null is empty, otherwise a list of strings.
fn selectors_of(chunk: &Value) -> Result<Vec<&str>> {
    match chunk.get("ledger_selectors") {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| match item.as_str() {
                Some(selector) => Ok(selector),
                None => bail!("a ledger selector is not a string"),
            })
            .collect(),
        Some(_) => bail!("a chunk's ledger_selectors is not a list"),
    }
}

/// `... N more <what>` when `total` is longer than the printed cap.
fn push_overflow(lines: &mut Vec<String>, total: usize, what: &str) {
    if total > LINE_CAP {
        lines.push(format!("... {} more {what}", total - LINE_CAP));
    }
}

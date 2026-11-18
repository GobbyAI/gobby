//! The status file docs/port/status/<chunk id>.json: its grammar, its vocabulary, and the row
//! checks that `check-slice` and `check-slice --report` share.

use std::collections::BTreeMap;

use serde::Deserialize;

use crate::STATUS_DIR;

/// The status vocabulary. Every refusal about a status file prints it.
pub const VOCABULARY: &str = "todo, specced, done, partial, stub, needs-local, deferred-local, \
     blocked(<why>), delegated, dropped";

/// The status of one row, as the vocabulary names it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum State {
    Todo,
    Specced,
    Done,
    Partial,
    Stub,
    NeedsLocal,
    DeferredLocal,
    Blocked,
    Delegated,
    Dropped,
}

impl State {
    /// The state a status word names, or None when the word is outside the vocabulary.
    pub fn parse(word: &str) -> Option<Self> {
        let state = match word {
            "todo" => Self::Todo,
            "specced" => Self::Specced,
            "done" => Self::Done,
            "partial" => Self::Partial,
            "stub" => Self::Stub,
            "needs-local" => Self::NeedsLocal,
            "deferred-local" => Self::DeferredLocal,
            "delegated" => Self::Delegated,
            "dropped" => Self::Dropped,
            _ if is_blocked(word) => Self::Blocked,
            _ => return None,
        };
        Some(state)
    }
}

/// Whether `word` is `blocked(<why>)` with a non-empty reason.
fn is_blocked(word: &str) -> bool {
    word.strip_prefix("blocked(")
        .and_then(|rest| rest.strip_suffix(')'))
        .is_some_and(|reason| !reason.trim().is_empty())
}

#[derive(Deserialize)]
struct Document {
    chunk: String,
    rows: Vec<RawRow>,
}

#[derive(Deserialize)]
struct RawRow {
    row: String,
    status: String,
    evidence: String,
}

/// One row of a parsed status file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    pub key: String,
    pub status: String,
    /// None when `status` is outside the vocabulary.
    pub state: Option<State>,
    pub evidence: String,
}

/// A parsed status file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Status {
    pub chunk: String,
    pub rows: Vec<Row>,
}

/// The repository path of `chunk_id`'s status file.
pub fn path(chunk_id: &str) -> String {
    format!("{STATUS_DIR}/{chunk_id}.json")
}

/// One refusal about a status file: its path, the detail, and the vocabulary.
pub fn refusal(path: &str, detail: &str) -> String {
    format!("{path}: {detail}; statuses are {VOCABULARY}")
}

/// Parses the bytes of a status file. A non-UTF-8 or malformed file is its error message.
pub fn parse(bytes: &[u8]) -> Result<Status, String> {
    let text = std::str::from_utf8(bytes).map_err(|error| error.to_string())?;
    let document: Document = serde_json::from_str(text).map_err(|error| error.to_string())?;
    let rows = document
        .rows
        .into_iter()
        .map(|raw| Row {
            state: State::parse(&raw.status),
            key: raw.row,
            status: raw.status,
            evidence: raw.evidence,
        })
        .collect();
    Ok(Status {
        chunk: document.chunk,
        rows,
    })
}

/// Refusals for a status file's rows against the rows `chunk_id` owns: a `chunk` field that names
/// another chunk, a status outside the vocabulary, a row still `todo` or `specced`, a repeated row,
/// a missing owned row, and a row the chunk does not own.
pub fn row_problems(path: &str, chunk_id: &str, status: &Status, owned: &[&str]) -> Vec<String> {
    let mut details = Vec::new();
    if status.chunk != chunk_id {
        details.push(format!("names chunk {:?}, not {chunk_id}", status.chunk));
    }
    let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
    for row in &status.rows {
        *counts.entry(row.key.as_str()).or_insert(0) += 1;
        match row.state {
            None => details.push(format!("row {} has status {:?}", row.key, row.status)),
            Some(State::Todo | State::Specced) => {
                details.push(format!("row {} is still {}", row.key, row.status));
            }
            Some(_) => {}
        }
    }
    for (key, count) in &counts {
        if *count > 1 {
            details.push(format!("row {key} appears {count} times"));
        }
        if !owned.contains(key) {
            details.push(format!("row {key} is not owned by {chunk_id}"));
        }
    }
    for key in owned {
        if !counts.contains_key(key) {
            details.push(format!("owned row {key} is missing"));
        }
    }
    details.iter().map(|detail| refusal(path, detail)).collect()
}

/// The two SHA tokens of a `red <sha> green <sha> test <command>` evidence line, or None when the
/// line has any other shape.
pub fn evidence_shas(evidence: &str) -> Option<(&str, &str)> {
    let tokens: Vec<&str> = evidence.split_whitespace().collect();
    match tokens.as_slice() {
        ["red", red, "green", green, "test", _, ..] => Some((*red, *green)),
        _ => None,
    }
}

#[cfg(test)]
#[path = "status/tests.rs"]
mod tests;

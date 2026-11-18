//! Ledger rows and the chunks whose selectors own them.
//!
//! This follows scripts/port/check_coverage.py: a row key is the text between
//! the leading "| `" and the first "` |" of a table line, taken as written, and
//! a selector owns a key when it matches anywhere in it.

use fancy_regex::Regex;

use crate::map::Chunk;

/// One table row of a ledger document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    /// The key as written in the table, with any `\|` escape kept.
    pub key: String,
    pub status: String,
}

/// Every table row of a ledger document, in file order.
pub fn rows(ledger: &str) -> Vec<Row> {
    ledger
        .lines()
        .filter_map(|line| {
            let rest = line.strip_prefix("| `")?;
            let end = rest.find("` |")?;
            let status = line
                .split(" | ")
                .nth(1)
                .map(|cell| cell.trim().to_owned())
                .unwrap_or_default();
            Some(Row {
                key: rest[..end].to_owned(),
                status,
            })
        })
        .collect()
}

/// Compiled selectors, in map order.
pub struct Selectors {
    compiled: Vec<(String, Regex)>,
    /// One `<chunk id>: <selector>: <error>` line per selector that did not compile.
    pub bad: Vec<String>,
}

impl Selectors {
    pub fn compile(chunks: &[Chunk]) -> Self {
        let mut compiled = Vec::new();
        let mut bad = Vec::new();
        for chunk in chunks {
            for selector in &chunk.ledger_selectors {
                match Regex::new(selector) {
                    Ok(pattern) => compiled.push((chunk.id.clone(), pattern)),
                    Err(error) => bad.push(format!("{}: {selector:?}: {error}", chunk.id)),
                }
            }
        }
        Self { compiled, bad }
    }

    /// The distinct chunk ids that own `key`, sorted.
    pub fn owners(&self, key: &str) -> Vec<&str> {
        let mut owners: Vec<&str> = self
            .compiled
            .iter()
            .filter(|(_, pattern)| pattern.is_match(key).unwrap_or(false))
            .map(|(id, _)| id.as_str())
            .collect();
        owners.sort_unstable();
        owners.dedup();
        owners
    }

    /// The keys that `chunk_id` alone owns, in the order given.
    pub fn owned_by<'a>(&self, chunk_id: &str, rows: &'a [Row]) -> Vec<&'a str> {
        rows.iter()
            .map(|row| row.key.as_str())
            .filter(|key| self.owners(key) == [chunk_id])
            .collect()
    }
}

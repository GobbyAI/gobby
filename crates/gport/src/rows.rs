//! Ledger rows and the chunks whose selectors own them.
//!
//! This follows scripts/port/check_coverage.py: a row key is the text between the leading "| `"
//! and the first "` |" of a table line, taken as written (no unescaping), and a selector owns a
//! key when it matches anywhere in it. `Selectors` is the one ownership engine, used by the ledger
//! and by record and slice.

use anyhow::Result;
use fancy_regex::Regex;

use crate::ledger::pyfmt::{py_splitlines, py_string_repr, py_strip};
use crate::ledger::pyre;
use crate::map::Chunk;

/// One table row of a ledger document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    /// The key as written in the table, with any `\|` escape kept.
    pub key: String,
    pub status: String,
}

/// Every table row of a ledger document, in file order. Lines are split as Python's
/// `splitlines` splits them, so a key holding a line separator is cut and is not a row.
pub fn rows(ledger: &str) -> Vec<Row> {
    py_splitlines(ledger)
        .into_iter()
        .filter_map(|line| {
            if !line.starts_with("| `") {
                return None;
            }
            let end = line.find("` |")?;
            let key = line.get(3..end).unwrap_or_default();
            let status = line
                .split(" | ")
                .nth(1)
                .map(|cell| py_strip(cell).to_owned())
                .unwrap_or_default();
            Some(Row {
                key: key.to_owned(),
                status,
            })
        })
        .collect()
}

/// One compiled selector: the chunk that holds it, the selector as written, and its pattern.
struct Compiled {
    chunk: String,
    selector: String,
    pattern: Regex,
}

/// Compiled selectors, in map order, each with the id of the chunk that holds it.
pub struct Selectors {
    compiled: Vec<Compiled>,
    /// One `<chunk id>: '<selector>': <message>` line per selector that does not compile.
    pub bad: Vec<String>,
}

impl Selectors {
    /// Compile every selector of every chunk, in map order.
    pub fn compile(chunks: &[Chunk]) -> Self {
        Self::from_selectors(chunks.iter().flat_map(|chunk| {
            chunk
                .ledger_selectors
                .iter()
                .map(move |selector| (chunk.id.as_str(), selector.as_str()))
        }))
    }

    /// Compile `(chunk id, selector)` pairs, in the order given.
    pub fn from_selectors<'a>(selectors: impl IntoIterator<Item = (&'a str, &'a str)>) -> Self {
        let mut compiled = Vec::new();
        let mut bad = Vec::new();
        for (id, selector) in selectors {
            match compile_selector(selector) {
                Ok(pattern) => compiled.push(Compiled {
                    chunk: id.to_owned(),
                    selector: selector.to_owned(),
                    pattern,
                }),
                Err(message) => bad.push(format!("{id}: {}: {message}", py_string_repr(selector))),
            }
        }
        Self { compiled, bad }
    }

    /// The distinct chunk ids that own `key`, sorted. A selector that fails to run is an error that
    /// names its chunk, its selector as written, and the key it failed on.
    pub fn try_owners(&self, key: &str) -> Result<Vec<&str>> {
        let mut owners = Vec::new();
        for entry in &self.compiled {
            match entry.pattern.is_match(key) {
                Ok(true) => owners.push(entry.chunk.as_str()),
                Ok(false) => {}
                Err(error) => {
                    return Err(anyhow::Error::new(error).context(format!(
                        "selector of {} failed on {key}: {}",
                        entry.chunk,
                        py_string_repr(&entry.selector)
                    )));
                }
            }
        }
        owners.sort_unstable();
        owners.dedup();
        Ok(owners)
    }

    /// The keys that `chunk_id` alone owns, in the order given. A selector that fails to run is an
    /// error.
    pub fn try_owned_by<'a>(&self, chunk_id: &str, rows: &'a [Row]) -> Result<Vec<&'a str>> {
        let mut owned = Vec::new();
        for row in rows {
            if self.try_owners(&row.key)? == [chunk_id] {
                owned.push(row.key.as_str());
            }
        }
        Ok(owned)
    }
}

/// A selector compiled for fancy-regex, or the message for why it is refused.
fn compile_selector(selector: &str) -> std::result::Result<Regex, String> {
    let pattern = pyre::translate(selector)?;
    Regex::new(&pattern).map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::{Row, Selectors};

    /// `alpha` runs out of backtracking on this key, so it fails at run time; `beta` matches it.
    /// The lookahead keeps the selector on the backtracking engine without a backreference, which
    /// `pyre` refuses.
    fn exhausting() -> Selectors {
        Selectors::from_selectors([("alpha", r"^(a+)+(?!z)!"), ("beta", "^a")])
    }

    fn key() -> String {
        format!("{}?", "a".repeat(40))
    }

    /// The strict owners surface the failing selector as an error.
    #[test]
    fn try_owners_surfaces_a_selector_that_fails_to_run() {
        assert!(exhausting().try_owners(&key()).is_err());
    }

    /// The error names the chunk, the selector as written and the key it failed on.
    #[test]
    fn try_owners_error_names_the_chunk_and_the_selector() {
        let message = format!("{:#}", exhausting().try_owners(&key()).unwrap_err());
        assert!(
            message.contains("alpha") && message.contains("^(a+)+") && message.contains("aaaa"),
            "{message}"
        );
    }

    /// The strict owned keys surface the failing selector as an error too.
    #[test]
    fn try_owned_by_surfaces_a_selector_that_fails_to_run() {
        let key = key();
        let rows = [Row {
            key: key.clone(),
            status: String::new(),
        }];
        assert!(exhausting().try_owned_by("beta", &rows).is_err());
    }
}

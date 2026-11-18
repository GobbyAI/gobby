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

/// Why a selector whose meaning is only guaranteed on ASCII keys fails on a non-ASCII key.
const ASCII_ONLY_REASON: &str = "the selector uses the ignore-case flag or one of \\w, \\W, \\b, \
    which read a non-ASCII key differently from Python";

/// One compiled selector: the chunk that holds it, the selector as written, its pattern, and
/// whether its meaning is only guaranteed on ASCII keys.
struct Compiled {
    chunk: String,
    selector: String,
    pattern: Regex,
    ascii_only: bool,
}

impl Compiled {
    /// Whether the selector matches `key`. A selector whose meaning is only guaranteed on ASCII
    /// keys refuses a non-ASCII key rather than answer with another meaning. Under the ignore-case
    /// flag, fancy-regex folds U+0131, U+0130, U+212A and U+017F differently from Python. Under a
    /// word escape it reads U+00B2, U+0301, U+0345 and U+203F differently.
    fn is_match(&self, key: &str) -> Result<bool> {
        if self.ascii_only && !key.is_ascii() {
            return Err(anyhow::anyhow!(ASCII_ONLY_REASON));
        }
        self.pattern.is_match(key).map_err(anyhow::Error::new)
    }
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
                Ok((pattern, ascii_only)) => compiled.push(Compiled {
                    chunk: id.to_owned(),
                    selector: selector.to_owned(),
                    pattern,
                    ascii_only,
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
            match entry.is_match(key) {
                Ok(true) => owners.push(entry.chunk.as_str()),
                Ok(false) => {}
                Err(error) => {
                    return Err(error.context(format!(
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

/// A selector compiled for fancy-regex, with whether its meaning is only guaranteed on ASCII keys,
/// or the message for why it is refused.
fn compile_selector(selector: &str) -> std::result::Result<(Regex, bool), String> {
    let translated = pyre::translate(selector)?;
    let pattern = Regex::new(&translated.pattern).map_err(|error| error.to_string())?;
    Ok((pattern, translated.ascii_only))
}

#[cfg(test)]
mod tests {
    use super::{Row, Selectors};
    use crate::ledger::pyfmt::py_string_repr;

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

    /// An ignore-case selector fails on a non-ASCII key, whatever the key holds: Python folds
    /// U+0131, U+0130, U+212A and U+017F under `(?i)`, and fancy-regex folds them differently. The
    /// error names the chunk, the selector and the key. ASCII keys are unaffected.
    #[test]
    fn ignore_case_selector_fails_on_a_non_ascii_key() {
        let selectors = Selectors::from_selectors([("alpha", "(?i)^k$")]);
        for key in ["\u{212a}", "\u{131}", "\u{130}", "\u{17f}", "\u{e9}"] {
            let message = format!("{:#}", selectors.try_owners(key).unwrap_err());
            assert!(
                message.contains("alpha") && message.contains("(?i)^k$") && message.contains(key),
                "{message}"
            );
        }
        assert_eq!(selectors.try_owners("K").unwrap(), vec!["alpha"]);
        assert!(selectors.try_owners("x").unwrap().is_empty());
    }

    /// The owners of `key`, or a panic that names the key and the error.
    fn owners(selectors: &Selectors, key: &str) -> Vec<String> {
        selectors
            .try_owners(key)
            .unwrap_or_else(|error| panic!("{key:?}: {error:#}"))
            .into_iter()
            .map(str::to_owned)
            .collect()
    }

    /// The ignore-case flag in either on-list form still refuses a non-ASCII key: `(?i:a)` and
    /// `(?i-m:A)` both fail on the dotless i, as they did before.
    #[test]
    fn ignore_case_on_list_still_refuses_non_ascii_keys() {
        for form in ["(?i:a)", "(?i-m:A)"] {
            let selectors = Selectors::from_selectors([("alpha", form)]);
            assert!(
                selectors.bad.is_empty(),
                "{form} is refused: {:?}",
                selectors.bad
            );
            assert!(
                selectors.try_owners("\u{131}").is_err(),
                "{form} answers a non-ASCII key"
            );
        }
    }

    /// A case-sensitive group, `(?-i:...)`, keeps Python's meaning on a non-ASCII key: the off-list
    /// `i` does not refuse the key. Verdicts from `python3.13 -I`: among `k`, `K`, `ı`, U+212A and
    /// `é`, each of `(?-i:k)`, `(?s-i:k)`, `(?-im:k)` and `(?u-i:k)` owns only `k`.
    #[test]
    fn ignore_case_turned_off_owns_non_ascii_keys_by_python_meaning() {
        for form in [r"(?-i:k)", r"(?s-i:k)", r"(?-im:k)", r"(?u-i:k)"] {
            let selectors = Selectors::from_selectors([("alpha", form)]);
            assert!(
                selectors.bad.is_empty(),
                "{form} is refused: {:?}",
                selectors.bad
            );
            for key in ["\u{131}", "K", "\u{212a}", "\u{e9}"] {
                assert!(owners(&selectors, key).is_empty(), "{form} owns {key:?}");
            }
            assert_eq!(owners(&selectors, "k"), ["alpha"], "{form}");
        }
    }

    /// `\w`, `\W` and `\b` read a non-ASCII key differently from Python. Python counts U+00B2 as a
    /// word character and U+0301, U+0345 and U+203F as not; fancy-regex does the reverse. So a
    /// selector with a word escape fails on a non-ASCII key, and the error names the chunk, the
    /// selector, the key and the reason.
    #[test]
    fn word_class_selector_fails_on_a_non_ascii_key() {
        for form in [r"\w", r"\W", r"\b", r"[\w]", r"[\W]", r"[^\w]", r"^\w+$"] {
            let selectors = Selectors::from_selectors([("alpha", form)]);
            assert!(
                selectors.bad.is_empty(),
                "{form} is refused: {:?}",
                selectors.bad
            );
            for key in ["\u{b2}", "\u{301}", "\u{345}", "\u{203f}"] {
                let message = match selectors.try_owners(key) {
                    Ok(owners) => panic!("{form} answers {owners:?} for {key:?}"),
                    Err(error) => format!("{error:#}"),
                };
                assert!(
                    message.contains("alpha")
                        && message.contains(key)
                        && message.contains(&py_string_repr(form))
                        && message.contains("read a non-ASCII key differently from Python"),
                    "{message}"
                );
            }
        }
    }

    /// On ASCII keys a word-class selector keeps Python's meaning: `^\w+$` owns `abc_1` and not
    /// `a-b`, as `re.search` reads them (`python3.13 -I`).
    #[test]
    fn word_class_selector_keeps_python_meaning_on_ascii_keys() {
        let selectors = Selectors::from_selectors([("alpha", r"^\w+$")]);
        assert_eq!(owners(&selectors, "abc_1"), ["alpha"]);
        assert!(owners(&selectors, "a-b").is_empty());
    }
}

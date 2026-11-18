//! Section 4: the modules that define a cache, with the invalidation calls each one holds.

use std::collections::BTreeSet;

use super::scan::{TextFile, tokens};
use super::table;

const HEADING: &str = "## 4. Caches";
const INVALIDATION: [&str; 3] = ["cache_clear(", "invalidate", "NOTIFY"];

pub fn section(python: &[&TextFile]) -> String {
    let mut rows = Vec::new();
    for file in python {
        let kinds = cache_kinds(&file.text);
        if kinds.is_empty() {
            continue;
        }
        let calls: Vec<&str> = INVALIDATION
            .into_iter()
            .filter(|call| file.text.contains(*call))
            .collect();
        let calls = if calls.is_empty() {
            "none found".to_owned()
        } else {
            calls.join(", ")
        };
        let kinds: Vec<String> = kinds.into_iter().collect();
        rows.push(vec![file.inner().to_owned(), kinds.join(", "), calls]);
    }
    if rows.is_empty() {
        return format!("{HEADING}\n\nNo module under src/gobby defines a cache.\n");
    }
    format!(
        "{HEADING}\n\nModules under src/gobby that define a cache (lru_cache, functools.cache, \
         TTLCache, or an attribute whose name ends in _cache), with the invalidation calls \
         found in the same module.\n\n{}",
        table(
            &["Module", "Cache definitions", "Invalidation calls"],
            &rows
        )
    )
}

/// The cache definitions in one module, as labels, sorted.
fn cache_kinds(text: &str) -> BTreeSet<String> {
    let mut kinds = BTreeSet::new();
    for token in tokens(text) {
        match (token.ident, token.qualifier) {
            ("lru_cache", _) => {
                kinds.insert("lru_cache".to_owned());
            }
            ("cache", Some("functools")) => {
                kinds.insert("functools.cache".to_owned());
            }
            ("TTLCache", _) => {
                kinds.insert("TTLCache".to_owned());
            }
            (name, Some(_)) if name.ends_with("_cache") => {
                kinds.insert(format!("attribute {name}"));
            }
            _ => {}
        }
    }
    kinds
}

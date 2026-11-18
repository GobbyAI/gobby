//! Section 4: the modules that define a cache, with the invalidation calls each one holds.

use std::collections::BTreeSet;

use super::scan::{TextFile, decorators, tokens};
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
         TTLCache, an attribute assigned under a name ending in _cache, or a decorator whose last \
         name part is cache or ends in _cache), with the invalidation calls found in the same \
         module.\n\n{}",
        table(
            &["Module", "Cache definitions", "Invalidation calls"],
            &rows
        )
    )
}

/// The cache definitions in one module, as labels, sorted. A dotted name ending in `_cache` counts
/// only as an assignment target (`self._cache = {}`, `self._cache: dict = {}`, or one name of
/// `self._a_cache, self._b_cache = {}, {}`). A call such as `self.get_cache()`, a method reference,
/// a read, a `.clear()`, a comparison, an augmented assignment, a keyword argument or a membership
/// test is a use, not a definition. A decorator whose name ends in `cache` or `_cache` counts called
/// or not.
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
            (name, Some(_)) if name.ends_with("_cache") && token.assigned => {
                kinds.insert(format!("attribute {name}"));
            }
            _ => {}
        }
    }
    for name in decorators(text) {
        if is_cache_decorator(name) {
            kinds.insert(format!("decorator {name}"));
        }
    }
    kinds
}

/// Whether a decorator's dotted name is a cache: its last component is `cache` or ends in `_cache`.
/// `lru_cache` and `functools.cache` are labelled by the token rules above, so they are not
/// repeated here.
fn is_cache_decorator(name: &str) -> bool {
    let last = name.rsplit('.').next().unwrap_or(name);
    let cache_named = last == "cache" || last.ends_with("_cache");
    cache_named && last != "lru_cache" && name != "functools.cache"
}

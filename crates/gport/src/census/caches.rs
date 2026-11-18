//! Section 4: the modules that define a cache, with the invalidation calls each one holds.

use std::collections::BTreeSet;

use super::scan::{TextFile, decorators, tokens};
use super::table;

const HEADING: &str = "## 4. Caches";

pub fn section(python: &[&TextFile]) -> String {
    let mut rows = Vec::new();
    for file in python {
        let kinds = cache_kinds(&file.text);
        if kinds.is_empty() {
            continue;
        }
        let calls = invalidation_calls(&file.text);
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
        "{HEADING}\n\nModules under src/gobby that define a cache: `lru_cache`, `functools.cache` or \
         `TTLCache` in code; a decorator whose last name part is `cache` or ends in `_cache`; or a \
         name whose last part ends in `_cache` and is assigned a value, as `x_cache = v`, \
         `a = x_cache = v`, `x_cache: T = v`, or one name of a tuple, list or parenthesised target \
         list. An attribute such as `self._x_cache` is labelled `attribute`, and a bare name `name`. \
         A bare name counts only where it starts a statement: after indentation, after a `;`, or \
         after a compound header's colon. An annotation with no value, a parameter, a keyword \
         argument, a lambda parameter, a for-loop target, a walrus, a comparison or a name in a \
         comment or string is not a definition. Each module is listed with the invalidation calls \
         found in the same module: `cache_clear(` for a code name ending in `cache_clear`, and \
         `invalidate` for a code name containing it, where a comment or string does not count; \
         `NOTIFY` for that word anywhere in the module text.\n\n{}",
        table(
            &["Module", "Cache definitions", "Invalidation calls"],
            &rows
        )
    )
}

/// The invalidation calls in one module, in report order. `cache_clear(` is found for a code name
/// that ends in `cache_clear`, and `invalidate` for a code name that contains it, so a comment or a
/// string that names one does not count. `NOTIFY` is matched in the raw text, because it is SQL in a
/// string and the token scan cannot tell a string from a comment.
fn invalidation_calls(text: &str) -> Vec<&'static str> {
    let code: Vec<&str> = tokens(text)
        .into_iter()
        .filter(|token| token.code)
        .map(|token| token.ident)
        .collect();
    let mut calls = Vec::new();
    if code.iter().any(|ident| ident.ends_with("cache_clear")) {
        calls.push("cache_clear(");
    }
    if code.iter().any(|ident| ident.contains("invalidate")) {
        calls.push("invalidate");
    }
    if text.contains("NOTIFY") {
        calls.push("NOTIFY");
    }
    calls
}

/// The cache definitions in one module, as labels, sorted. A name ending in `_cache` is a definition
/// when it is the last name of an assignment target in code: `x_cache = v`, `a = x_cache = v`,
/// `x_cache: T = v`, or one name of a tuple, list or parenthesised target list, dotted or bare. A
/// bare name is one only where its statement starts. A call such as `self.get_cache()`, a method
/// reference, a read, a `.clear()`, a comparison, an augmented assignment, a keyword argument, a
/// lambda parameter, a for-loop target, a walrus or an annotation with no value is a use, not a
/// definition. A decorator whose name ends in `cache` or `_cache` counts called or not. Names in a
/// comment or a string count for nothing.
fn cache_kinds(text: &str) -> BTreeSet<String> {
    let mut kinds = BTreeSet::new();
    for token in tokens(text) {
        if !token.code {
            continue;
        }
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
            (name, None) if name.ends_with("_cache") && token.assigned => {
                kinds.insert(format!("name {name}"));
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

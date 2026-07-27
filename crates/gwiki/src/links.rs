//! Wikilink extraction and target normalization.
//!
//! The implementation lives in `gobby_core::vault::links` (#17514) so gcode
//! and gwiki resolve link targets with identical grammar; this module
//! re-exports the pieces gwiki consumes under the crate-local path every
//! consumer already uses.

pub use gobby_core::vault::links::{
    LinkKind, WikiLink, canonical_target_key, extract_links, extract_links_with_canonical_targets,
};

/// Entity-shaped keys (no path separator) are what upkeep clusters into
/// entity pages; path-shaped targets — including `knowledge/sources/...`
/// digest keys — can never converge that way. Upkeep's cluster accumulation
/// and librarian's broken-link classification must share this predicate, or
/// upkeep mints junk pages librarian already wrote off as repair debt
/// (#17652).
pub(crate) fn is_entity_key(key: &str) -> bool {
    !key.is_empty() && !key.contains('/')
}

const GENERIC_CONCEPT_WORDS: &[&str] = &[
    "action",
    "actions",
    "artifact",
    "artifacts",
    "awk",
    "cat",
    "config",
    "configuration",
    "cut",
    "data",
    "detail",
    "details",
    "doc",
    "docs",
    "document",
    "entry",
    "error",
    "example",
    "file",
    "files",
    "flag",
    "grep",
    "head",
    "info",
    "input",
    "item",
    "items",
    "link",
    "links",
    "list",
    "log",
    "logs",
    "message",
    "name",
    "note",
    "notes",
    "output",
    "page",
    "pages",
    "path",
    "paths",
    "plan",
    "record",
    "records",
    "result",
    "results",
    "script",
    "sed",
    "sort",
    "status",
    "step",
    "steps",
    "string",
    "tail",
    "task",
    "test",
    "tr",
    "uniq",
    "value",
    "values",
    "warning",
    "wc",
];

/// Return a stable, compact reason when a canonical entity key should not
/// become a concept. Rules stay deterministic so upkeep dry-runs and applied
/// runs report identical decisions.
pub(crate) fn concept_rejection_reason(key: &str) -> Option<&'static str> {
    let key = key.trim();
    if key.chars().count() < 2 {
        return Some("too_short");
    }
    if key.bytes().all(|byte| byte.is_ascii_digit()) {
        return Some("bare_numeric");
    }
    if !key.chars().next().is_some_and(char::is_alphanumeric) {
        return Some("leading_non_alphanumeric");
    }
    if !is_entity_key(key) {
        return Some("path_shaped");
    }

    let normalized = key.to_ascii_lowercase();
    if is_artifact_id(&normalized) {
        return Some("artifact_id");
    }
    if normalized.ends_with("-concept") || normalized.ends_with("-page") {
        return Some("positional_suffix");
    }
    if GENERIC_CONCEPT_WORDS.contains(&normalized.as_str()) {
        return Some("generic_word");
    }
    None
}

pub(crate) fn is_concept_worthy(key: &str) -> bool {
    concept_rejection_reason(key).is_none()
}

fn is_artifact_id(key: &str) -> bool {
    ["task", "issue", "pr", "bug", "ticket", "designconstraint"]
        .into_iter()
        .any(|prefix| {
            let Some(rest) = key.strip_prefix(prefix) else {
                return false;
            };
            let digits = rest.strip_prefix(['-', '_', '#']).unwrap_or(rest);
            !digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit())
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn concept_worthiness_accepts_technical_terms_and_rejects_artifacts() {
        for key in [
            "sha256", "x509", "iso8601", "rfc3339", "fts5", "bm25", "http2", "falkordb", "session",
            "daemon", "vault", "agy",
        ] {
            assert!(is_concept_worthy(key), "expected `{key}` to be worthy");
            assert_eq!(concept_rejection_reason(key), None, "{key}");
        }

        for (key, reason) in [
            ("a", "too_short"),
            ("42", "bare_numeric"),
            ("_context", "leading_non_alphanumeric"),
            ("designconstraint802", "artifact_id"),
            ("issue861", "artifact_id"),
            ("task-16289", "artifact_id"),
            ("widget-concept", "positional_suffix"),
            ("widget-page", "positional_suffix"),
            ("page", "generic_word"),
            ("awk", "generic_word"),
            ("sed", "generic_word"),
            ("grep", "generic_word"),
            ("src/gobby/tasks/store.py", "path_shaped"),
        ] {
            assert!(!is_concept_worthy(key), "expected `{key}` to be rejected");
            assert_eq!(concept_rejection_reason(key), Some(reason), "{key}");
        }
    }
}

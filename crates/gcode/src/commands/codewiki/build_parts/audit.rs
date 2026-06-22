//! Deterministic codewiki audit page (#889): deprecation markers. Built once
//! per run from the existing code index facts (symbols) plus a bounded source
//! scan — no LLM, additive, and NEVER degrading (the page always renders, even
//! when there is nothing to report).
//!
//! Detection is done here at generation time by scanning the on-disk source
//! (mirroring `system_model`'s `std::fs::read_to_string`), so the index parser
//! and the hub schema are untouched.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use super::super::{CodewikiInput, DeprecatedSymbol, DeprecationIndex, DeprecationsDoc, TestIndex};
use crate::models::Symbol;

/// How many source lines above a symbol the scan inspects for deprecation
/// attributes / doc-comments and test gating. Generous enough to clear a short
/// doc-comment block plus a multi-line `#[deprecated(...)]` attribute.
const LOOKBACK_LINES: usize = 12;

/// Max length of a `DEPRECATED` doc-comment reason kept for the badge/page.
const REASON_MAX: usize = 160;

/// Bundled deterministic audit state threaded from `run.rs` through the
/// generation core, mirroring how `system_model`/`feature_catalog` are passed
/// as `Option<&_>`. Built once; carries the deprecation index (for the badge +
/// the deprecations page) and the test-gated symbol index (for the file page).
pub(crate) struct AuditContext {
    pub(crate) deprecations: DeprecationIndex,
    /// Ids of every test-gated symbol, so the file page can collapse tests into
    /// a single count instead of one `## Reference` row each. Built by the same
    /// bounded source scan as `deprecations`.
    pub(crate) tests: TestIndex,
}

/// Build the audit context once per run from the project root and the codewiki
/// input.
pub(crate) fn build_audit_context(project_root: &Path, input: &CodewikiInput) -> AuditContext {
    AuditContext {
        deprecations: build_deprecation_index(project_root, input),
        tests: build_test_index(project_root, input),
    }
}

/// Scan the documented source once and collect the ids of every test-gated
/// symbol (a `#[test]`/`#[cfg(test)]` attribute above it, or a tests path).
/// Lets the file page collapse tests into a single count instead of one
/// `## Reference` row each. Files that cannot be read contribute nothing.
fn build_test_index(project_root: &Path, input: &CodewikiInput) -> TestIndex {
    let mut index: TestIndex = BTreeSet::new();
    let mut file_cache: BTreeMap<&str, Option<Vec<String>>> = BTreeMap::new();
    for symbol in &input.symbols {
        if is_test_gated(symbol, project_root, &mut file_cache) {
            index.insert(symbol.id.clone());
        }
    }
    index
}

/// Scan the documented source files once and map `symbol.id -> reason` for every
/// symbol carrying a deprecation marker. Files that cannot be read are skipped
/// (no panic, no degrade).
fn build_deprecation_index(project_root: &Path, input: &CodewikiInput) -> DeprecationIndex {
    let mut index: DeprecationIndex = BTreeMap::new();
    // Group symbols by file so each file is read at most once.
    let mut symbols_by_file: BTreeMap<&str, Vec<&Symbol>> = BTreeMap::new();
    for symbol in &input.symbols {
        symbols_by_file
            .entry(symbol.file_path.as_str())
            .or_default()
            .push(symbol);
    }
    for (file, symbols) in symbols_by_file {
        let Ok(contents) = std::fs::read_to_string(project_root.join(file)) else {
            continue;
        };
        let lines = contents.lines().collect::<Vec<_>>();
        for symbol in symbols {
            if let Some(reason) = deprecation_reason(&lines, symbol) {
                index.insert(symbol.id.clone(), reason);
            }
        }
    }
    index
}

/// Inspect the look-back lines above a symbol plus its docstring for a
/// deprecation marker. An attribute reason wins over a doc-comment reason.
fn deprecation_reason(lines: &[&str], symbol: &Symbol) -> Option<String> {
    let lookback = lookback_lines(lines, symbol.line_start);
    let attr_reason = deprecated_attribute_reason(&lookback);
    if let Some(reason) = attr_reason {
        return Some(reason);
    }
    if let Some(reason) = doc_comment_deprecation(&lookback) {
        return Some(reason);
    }
    // Fall back to the symbol's own docstring (e.g. Python-style) for a
    // `DEPRECATED` mention.
    if let Some(docstring) = &symbol.docstring
        && docstring.to_ascii_uppercase().contains("DEPRECATED")
    {
        return Some(cap_reason(first_meaningful_line(docstring)));
    }
    None
}

/// Collect the source lines immediately above a 1-based `line_start`, nearest
/// first, stopping at a blank line that is not part of an attribute / doc-comment
/// block. Returns lines in source order (top to bottom) for easy scanning.
fn lookback_lines<'a>(lines: &[&'a str], line_start: usize) -> Vec<&'a str> {
    let mut collected: Vec<&str> = Vec::new();
    // `line_start` is 1-based; the line above it is index `line_start - 2`.
    let mut idx = line_start.saturating_sub(2);
    let mut steps = 0;
    loop {
        if steps >= LOOKBACK_LINES || line_start < 2 {
            break;
        }
        let Some(line) = lines.get(idx) else {
            break;
        };
        let trimmed = line.trim_start();
        let is_attr_or_doc = trimmed.starts_with('#')
            || trimmed.starts_with("///")
            || trimmed.starts_with("//!")
            || trimmed.starts_with("//")
            || trimmed.starts_with('"')
            || trimmed.starts_with(')')
            || trimmed.starts_with('@');
        if trimmed.is_empty() {
            // A blank line ends the contiguous attr/doc block above the symbol.
            break;
        }
        // Keep collecting attr/doc-ish lines; a code line that is not part of an
        // attribute block also ends the block, but we still capture multi-line
        // attribute continuations (which may not start with `#`).
        collected.push(line);
        if !is_attr_or_doc && !looks_like_attribute_continuation(trimmed) {
            break;
        }
        if idx == 0 {
            break;
        }
        idx -= 1;
        steps += 1;
    }
    collected.reverse();
    collected
}

/// Heuristic for a line that continues a multi-line `#[deprecated(...)]`
/// attribute (e.g. a `note = "..."` argument on its own line).
fn looks_like_attribute_continuation(trimmed: &str) -> bool {
    trimmed.contains("note")
        || trimmed.contains("since")
        || trimmed.starts_with(')')
        || trimmed.ends_with(',')
        || trimmed.ends_with(')')
}

/// Detect a `#[deprecated]` / `#[deprecated(...)]` attribute in the look-back
/// block (possibly spanning lines) and return its `note="..."` if present, else
/// the literal `"deprecated"`.
fn deprecated_attribute_reason(lookback: &[&str]) -> Option<String> {
    let joined = lookback.join(" ");
    if !joined.contains("#[deprecated") && !joined.contains("# [deprecated") {
        return None;
    }
    if let Some(note) = extract_note(&joined) {
        return Some(cap_reason(note));
    }
    Some("deprecated".to_string())
}

/// Pull the value of `note = "..."` out of a (possibly multi-line, joined)
/// attribute string.
fn extract_note(joined: &str) -> Option<String> {
    let note_at = joined.find("note")?;
    let after = &joined[note_at..];
    let eq = after.find('=')?;
    let rest = &after[eq + 1..];
    let open = rest.find('"')?;
    let tail = &rest[open + 1..];
    let close = tail.find('"')?;
    let note = tail[..close].trim();
    if note.is_empty() {
        None
    } else {
        Some(note.to_string())
    }
}

/// Detect a `///` / `//!` doc-comment line mentioning the word `DEPRECATED`
/// (case-insensitive) and return that line's trimmed text (capped).
fn doc_comment_deprecation(lookback: &[&str]) -> Option<String> {
    for line in lookback {
        let trimmed = line.trim_start();
        let is_doc = trimmed.starts_with("///") || trimmed.starts_with("//!");
        if is_doc && trimmed.to_ascii_uppercase().contains("DEPRECATED") {
            let text = trimmed
                .trim_start_matches("///")
                .trim_start_matches("//!")
                .trim();
            return Some(cap_reason(text.to_string()));
        }
    }
    None
}

fn first_meaningful_line(text: &str) -> String {
    text.lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .unwrap_or("")
        .to_string()
}

fn cap_reason(mut reason: String) -> String {
    if reason.chars().count() > REASON_MAX {
        reason = reason.chars().take(REASON_MAX).collect::<String>();
        reason.push('…');
    }
    reason
}

/// Build the deterministic deprecations aggregate page model. Lists every
/// deprecated symbol grouped by file (the renderer groups). Never degrades.
pub(crate) fn build_deprecations_doc(
    input: &CodewikiInput,
    deprecations: &DeprecationIndex,
) -> DeprecationsDoc {
    let mut symbols = input
        .symbols
        .iter()
        .filter_map(|symbol| {
            deprecations.get(&symbol.id).map(|reason| DeprecatedSymbol {
                file: symbol.file_path.clone(),
                name: symbol.name.clone(),
                kind: symbol.kind.clone(),
                line: symbol.line_start,
                reason: reason.clone(),
            })
        })
        .collect::<Vec<_>>();
    symbols.sort_by(|a, b| {
        a.file
            .cmp(&b.file)
            .then(a.line.cmp(&b.line))
            .then(a.name.cmp(&b.name))
    });
    DeprecationsDoc {
        symbols,
        degraded_sources: Vec::new(),
    }
}

/// A symbol is test-gated when it lives under a tests path, or a `#[test]` /
/// `#[cfg(test)]` attribute appears in the look-back above it (reusing the
/// Part-1 scan). The per-file source is read once and cached.
fn is_test_gated<'a>(
    symbol: &'a Symbol,
    project_root: &Path,
    file_cache: &mut BTreeMap<&'a str, Option<Vec<String>>>,
) -> bool {
    if is_test_path(&symbol.file_path) {
        return true;
    }
    let lines = file_cache
        .entry(symbol.file_path.as_str())
        .or_insert_with(|| {
            std::fs::read_to_string(project_root.join(&symbol.file_path))
                .ok()
                .map(|contents| contents.lines().map(str::to_string).collect::<Vec<_>>())
        });
    let Some(lines) = lines else {
        return false;
    };
    let refs = lines.iter().map(String::as_str).collect::<Vec<_>>();
    let lookback = lookback_lines(&refs, symbol.line_start);
    lookback.iter().any(|line| {
        let trimmed = line.trim_start();
        trimmed.contains("#[test]")
            || trimmed.contains("#[cfg(test)]")
            || trimmed.contains("#[tokio::test")
    })
}

fn is_test_path(file: &str) -> bool {
    file.contains("/tests") || file.ends_with("tests.rs") || file.ends_with("test.rs")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lines(src: &str) -> Vec<&str> {
        src.lines().collect()
    }

    #[test]
    fn lookback_stops_at_blank_line() {
        // line 1: `fn far`, line 2: blank, line 3: `#[deprecated]`, line 4:
        // `fn target` — so the symbol's 1-based `line_start` is 4.
        let src = "fn far() {}\n\n#[deprecated]\nfn target() {}\n";
        let back = lookback_lines(&lines(src), 4);
        assert!(back.iter().any(|l| l.contains("#[deprecated]")));
        assert!(!back.iter().any(|l| l.contains("fn far")));
    }

    #[test]
    fn extracts_note_from_attribute() {
        assert_eq!(
            extract_note(r#"#[deprecated(note = "use new_fn")]"#).as_deref(),
            Some("use new_fn")
        );
    }

    #[test]
    fn bare_deprecated_attribute_reason_is_deprecated() {
        let back = ["#[deprecated]"];
        assert_eq!(
            deprecated_attribute_reason(&back).as_deref(),
            Some("deprecated")
        );
    }

    #[test]
    fn doc_comment_deprecated_is_detected_case_insensitively() {
        let back = ["/// deprecated: prefer the new API"];
        let reason = doc_comment_deprecation(&back).expect("doc deprecation");
        assert!(reason.to_ascii_lowercase().contains("prefer the new api"));
    }
}

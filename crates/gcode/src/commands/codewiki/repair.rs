//! Citation repair: re-anchors a generated vault's `[file:line]` citations
//! against the current index without regenerating any page. When source files
//! drift, the cheaper fix than a full LLM regen is to move each citation to its
//! symbol's current span. This module owns the deterministic, no-LLM repair
//! routine; the public `codewiki --repair-citations` flag wired in a later leaf
//! drives [`repair_citations`].

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use serde::Serialize;

use super::io::{read_codewiki_meta, safe_doc_path, write_doc};
use super::{CitationResolver, CodewikiIndexSnapshot, reanchor_citations};
use crate::models::Symbol;

/// Result of a citation-repair run. This is the source-of-truth serialized
/// shape that the gcode contract freezes for `codewiki --repair-citations`
/// (Leaf 5 / #876): a stable key set with no `dry_run` mode.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct CitationRepairSummary {
    /// On-disk pages read and checked for stale citations.
    pub pages_scanned: usize,
    /// Pages whose citations changed and were rewritten in place.
    pub pages_repaired: usize,
    /// Individual citations moved to a symbol's current span.
    pub citations_repaired: usize,
    /// Stale citations whose symbol no longer resolves in the current index;
    /// left in place so a human can investigate rather than silently dropped.
    pub citations_unresolved: usize,
}

/// Bridges on-disk citations to the current index. A citation is identified by
/// the persisted snapshot (which records each symbol's `line_start` at the
/// generation that wrote the page), then re-anchored to that same symbol's
/// span in the current index. Symbol IDs are UUID5 over `byte_start`, so they
/// change whenever a symbol moves; the stable identity across a move is
/// `(file, qualified_name, kind)`, which is what links the snapshot entry to a
/// current symbol.
struct IndexCitationResolver {
    /// Current symbol spans grouped by file, for the "is this still accurate?"
    /// containment check.
    current_spans: BTreeMap<String, Vec<(usize, usize)>>,
    /// `(file, qualified_name, kind)` -> current `(line_start, line_end)`.
    current_by_identity: BTreeMap<(String, String, String), (usize, usize)>,
    /// `(file, snapshot line_start)` -> `(qualified_name, kind)`, taken from the
    /// persisted snapshot. A `(file, line_start)` shared by two snapshot symbols
    /// is ambiguous and dropped, so an ambiguous citation stays unresolved
    /// rather than re-anchoring to the wrong symbol.
    snapshot_anchor: BTreeMap<(String, usize), (String, String)>,
}

impl IndexCitationResolver {
    /// Builds a resolver from the current symbol set and the snapshot persisted
    /// when the vault was last generated.
    fn build(symbols: &[Symbol], snapshot: &CodewikiIndexSnapshot) -> Self {
        let mut current_spans: BTreeMap<String, Vec<(usize, usize)>> = BTreeMap::new();
        let mut current_by_identity = BTreeMap::new();
        for symbol in symbols {
            current_spans
                .entry(symbol.file_path.clone())
                .or_default()
                .push((symbol.line_start, symbol.line_end));
            current_by_identity.insert(
                (
                    symbol.file_path.clone(),
                    symbol.qualified_name.clone(),
                    symbol.kind.clone(),
                ),
                (symbol.line_start, symbol.line_end),
            );
        }

        let mut snapshot_anchor: BTreeMap<(String, usize), (String, String)> = BTreeMap::new();
        let mut ambiguous: BTreeSet<(String, usize)> = BTreeSet::new();
        for snap in snapshot.symbols.values() {
            let key = (snap.file_path.clone(), snap.line_start);
            if snapshot_anchor
                .insert(
                    key.clone(),
                    (snap.qualified_name.clone(), snap.kind.clone()),
                )
                .is_some()
            {
                ambiguous.insert(key);
            }
        }
        for key in ambiguous {
            snapshot_anchor.remove(&key);
        }

        Self {
            current_spans,
            current_by_identity,
            snapshot_anchor,
        }
    }
}

impl CitationResolver for IndexCitationResolver {
    fn is_current(&self, file: &str, line_start: usize, line_end: usize) -> bool {
        self.current_spans.get(file).is_some_and(|spans| {
            spans
                .iter()
                .any(|(start, end)| *start <= line_start && line_end <= *end)
        })
    }

    fn resolve(&self, file: &str, line_start: usize) -> Option<(usize, usize)> {
        let (qualified_name, kind) = self.snapshot_anchor.get(&(file.to_string(), line_start))?;
        self.current_by_identity
            .get(&(file.to_string(), qualified_name.clone(), kind.clone()))
            .copied()
    }
}

/// Re-anchors every generated page's citations against `resolver`, rewriting
/// only pages whose citations changed. Reads the page set from the vault meta
/// log and each page from disk; never calls a generator or an LLM.
fn repair_with_resolver(
    out_dir: &Path,
    resolver: &dyn CitationResolver,
) -> anyhow::Result<CitationRepairSummary> {
    let meta = read_codewiki_meta(out_dir)?;
    let mut summary = CitationRepairSummary::default();
    for doc_path in meta.docs.keys() {
        let target = safe_doc_path(out_dir, doc_path)?;
        let content = match std::fs::read_to_string(&target) {
            Ok(content) => content,
            // A meta entry without a page on disk is stale bookkeeping, not a
            // page to repair; skip it.
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => continue,
            Err(err) => return Err(err.into()),
        };
        summary.pages_scanned += 1;
        let result = reanchor_citations(&content, resolver);
        summary.citations_repaired += result.repaired;
        summary.citations_unresolved += result.unresolved;
        if result.repaired > 0 && result.text != content {
            write_doc(out_dir, doc_path, &result.text)?;
            summary.pages_repaired += 1;
        }
    }
    Ok(summary)
}

/// Public entry: re-anchors every generated page's citations against the
/// current `symbols`, using the index snapshot persisted in the vault meta to
/// identify which symbol each stale citation named. No regeneration, no LLM —
/// this is the routine the `codewiki --repair-citations` flag (Leaf 5 / #876)
/// drives. A vault with no persisted snapshot (`unwrap_or_default`) cannot
/// identify moved symbols, so its stale citations all count as unresolved.
pub fn repair_citations(
    out_dir: &Path,
    symbols: &[Symbol],
) -> anyhow::Result<CitationRepairSummary> {
    let snapshot = read_codewiki_meta(out_dir)?
        .index_snapshot
        .unwrap_or_default();
    let resolver = IndexCitationResolver::build(symbols, &snapshot);
    repair_with_resolver(out_dir, &resolver)
}

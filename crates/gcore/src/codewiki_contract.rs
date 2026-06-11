//! Frontmatter contract for codewiki-generated wiki vault pages.
//!
//! gcode's `codewiki` command family is the producer: it writes generated
//! Markdown pages — YAML frontmatter, `[[target|label]]` wikilinks, and
//! degradation fields — into the gwiki vault. gwiki re-parses those
//! conventions in its frontmatter/indexer/audit/librarian paths. Producer and
//! consumer live in different crates, so the shared vocabulary and a golden
//! fixture are pinned here and checked by both crates' tests; the
//! human-readable contract lives in `docs/contracts/gwiki-cli.md`.

/// Frontmatter keys codewiki emits and gcode/gwiki read back.
pub const TITLE_KEY: &str = "title";
pub const TYPE_KEY: &str = "type";
pub const PROVENANCE_KEY: &str = "provenance";
pub const PROVENANCE_FILE_KEY: &str = "file";
pub const PROVENANCE_RANGES_KEY: &str = "ranges";
pub const GENERATED_BY_KEY: &str = "generated_by";
pub const TRUST_KEY: &str = "trust";
pub const FRESHNESS_KEY: &str = "freshness";
pub const DEGRADED_KEY: &str = "degraded";
pub const DEGRADED_SOURCES_KEY: &str = "degraded_sources";

/// Marker values codewiki writes for the keys above.
pub const GENERATED_BY_CODEWIKI: &str = "gcode-codewiki";
pub const TRUST_GENERATED: &str = "generated";
pub const FRESHNESS_INDEXED: &str = "indexed";

/// Golden codewiki page. The frontmatter block is byte-for-byte what gcode's
/// `frontmatter_with_degradation` emits for a degraded file page (sorted
/// provenance files, collapsed `start-end` ranges, single-line ranges as bare
/// numbers); the body shows the `[[target|label]]` wikilink convention. gcode
/// pins its emitter against this fixture and gwiki pins its parser against it,
/// so a change on either side fails that side's tests instead of drifting.
pub const GOLDEN_PAGE: &str = "---
title: src/lib.rs
type: file
provenance:
- file: src/lib.rs
  ranges:
  - 1-2
  - '10'
generated_by: gcode-codewiki
trust: generated
freshness: indexed
degraded: true
degraded_sources:
- model_provider_unavailable
---

# src/lib.rs

See [[code/files/src/lib.rs|src/lib.rs]].
";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn golden_page_carries_every_contract_key_and_marker_value() {
        for key in [
            TITLE_KEY,
            TYPE_KEY,
            PROVENANCE_KEY,
            PROVENANCE_FILE_KEY,
            PROVENANCE_RANGES_KEY,
            GENERATED_BY_KEY,
            TRUST_KEY,
            FRESHNESS_KEY,
            DEGRADED_KEY,
            DEGRADED_SOURCES_KEY,
        ] {
            assert!(
                GOLDEN_PAGE.contains(&format!("{key}:")),
                "golden page is missing contract key `{key}`"
            );
        }
        assert!(GOLDEN_PAGE.contains(GENERATED_BY_CODEWIKI));
        assert!(GOLDEN_PAGE.contains(TRUST_GENERATED));
        assert!(GOLDEN_PAGE.contains(FRESHNESS_INDEXED));
        assert!(GOLDEN_PAGE.contains("[[code/files/src/lib.rs|src/lib.rs]]"));
    }
}

//! Grounded verification pass for generated codewiki narrative.
//!
//! Generation can drift from the cited source. This module runs an optional
//! second pass over a generated page body: split the prose into numbered
//! blocks, ask the verifier which blocks are unsupported by the cited source
//! excerpts, and strip those original blocks. The verifier only ever returns
//! block IDs — it never rewrites prose — so a verifier cannot launder a
//! hallucination into the page. The deterministic block split, response parse,
//! and strip live here (no AI dependency) so they are unit-testable, and the
//! [`super::resolve_text_verifier`] closure is just the model call.

use crate::commands::codewiki::TextVerifier;
use crate::commands::codewiki::prompts::{self, SourceExcerpt};

/// Outcome of one verification pass over generated prose.
pub(crate) enum VerifyOutcome {
    /// No verifier ran (not configured, unavailable, or nothing to verify):
    /// proceed with the original text, undegraded.
    Skipped,
    /// The verifier ran and the page survives. `text` is the surviving prose
    /// (the full original when nothing was stripped); `degraded` is true when
    /// one or more unsupported blocks were removed.
    Verified { text: String, degraded: bool },
    /// The verifier ran but its output was unusable (no parseable verdict), or
    /// stripping removed every block: the caller falls back to the structural
    /// body and records degraded.
    Unusable,
}

/// Run the optional verification pass over `text`, grounding it against
/// `sources`. Failure-rule mapping:
/// - no verifier / verifier unavailable / nothing to verify -> [`VerifyOutcome::Skipped`]
/// - all blocks supported -> `Verified { degraded: false }`
/// - some blocks stripped (survivors remain) -> `Verified { degraded: true }`
/// - garbage verdict, or every block stripped -> [`VerifyOutcome::Unusable`]
pub(crate) fn verify_and_strip(
    verify: &mut Option<&mut TextVerifier<'_>>,
    text: &str,
    sources: &[SourceExcerpt],
) -> VerifyOutcome {
    let Some(verify) = verify.as_deref_mut() else {
        return VerifyOutcome::Skipped;
    };
    let blocks = split_blocks(text);
    if blocks.is_empty() {
        return VerifyOutcome::Skipped;
    }
    let prompt = prompts::verify_prompt(&blocks, sources);
    let Some(response) = verify(&prompt, prompts::VERIFY_SYSTEM) else {
        // Verifier unavailable (routed off / transport failure): skip, undegraded.
        return VerifyOutcome::Skipped;
    };
    let Some(unsupported) = parse_unsupported_ids(&response, blocks.len()) else {
        // Empty/garbage verifier response: structural fallback + degraded.
        return VerifyOutcome::Unusable;
    };
    if unsupported.is_empty() {
        return VerifyOutcome::Verified {
            text: text.to_string(),
            degraded: false,
        };
    }
    let survivors = strip_blocks(&blocks, &unsupported);
    if survivors.trim().is_empty() {
        return VerifyOutcome::Unusable;
    }
    VerifyOutcome::Verified {
        text: survivors,
        degraded: true,
    }
}

/// Split generated markdown into blank-line-separated blocks. Each block is the
/// unit the verifier numbers and the caller may strip, so this split and the
/// numbering in [`prompts::verify_prompt`] must stay in lockstep.
pub(crate) fn split_blocks(text: &str) -> Vec<String> {
    text.split("\n\n")
        .map(str::trim)
        .filter(|block| !block.is_empty())
        .map(str::to_string)
        .collect()
}

/// Parse the verifier's JSON-array response into the 1-based block IDs to strip.
/// Returns `None` when no JSON array is present (garbage/empty response),
/// `Some(vec![])` when the array is empty (every block supported). Out-of-range
/// and duplicate IDs are dropped.
pub(crate) fn parse_unsupported_ids(response: &str, block_count: usize) -> Option<Vec<usize>> {
    let start = response.find('[')?;
    let end = response[start..].find(']')? + start;
    let ids = serde_json::from_str::<Vec<i64>>(&response[start..=end]).ok()?;
    let mut filtered = ids
        .into_iter()
        .filter_map(|id| usize::try_from(id).ok())
        .filter(|id| (1..=block_count).contains(id))
        .collect::<Vec<_>>();
    filtered.sort_unstable();
    filtered.dedup();
    Some(filtered)
}

/// Rejoin the blocks whose 1-based index is not in `unsupported`, preserving
/// order and blank-line separation.
pub(crate) fn strip_blocks(blocks: &[String], unsupported: &[usize]) -> String {
    blocks
        .iter()
        .enumerate()
        .filter(|(index, _)| !unsupported.contains(&(index + 1)))
        .map(|(_, block)| block.as_str())
        .collect::<Vec<_>>()
        .join("\n\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::codewiki::TextVerifier;

    fn sources() -> Vec<SourceExcerpt> {
        vec![SourceExcerpt {
            path: "src/lib.rs".to_string(),
            line_start: 1,
            line_end: 4,
            excerpt: "pub fn run() {}".to_string(),
        }]
    }

    #[test]
    fn split_blocks_separates_on_blank_lines() {
        let blocks = split_blocks("## Purpose\n\nFirst para.\n\nSecond para.\n");
        assert_eq!(blocks, ["## Purpose", "First para.", "Second para."]);
    }

    #[test]
    fn parse_unsupported_ids_reads_json_array() {
        assert_eq!(parse_unsupported_ids("[2, 3]", 5), Some(vec![2, 3]));
        assert_eq!(parse_unsupported_ids("[]", 5), Some(vec![]));
        // Surrounding prose is tolerated; only the array is read.
        assert_eq!(parse_unsupported_ids("Unsupported: [1]", 5), Some(vec![1]));
        // Out-of-range and duplicate IDs are dropped.
        assert_eq!(parse_unsupported_ids("[0, 2, 2, 9]", 3), Some(vec![2]));
    }

    #[test]
    fn parse_unsupported_ids_rejects_garbage() {
        assert_eq!(parse_unsupported_ids("all good", 5), None);
        assert_eq!(parse_unsupported_ids("", 5), None);
        assert_eq!(parse_unsupported_ids("[1, 2", 5), None);
    }

    #[test]
    fn strip_blocks_drops_only_listed_ids() {
        let blocks = split_blocks("A\n\nB\n\nC");
        assert_eq!(strip_blocks(&blocks, &[2]), "A\n\nC");
        assert_eq!(strip_blocks(&blocks, &[]), "A\n\nB\n\nC");
    }

    #[test]
    fn verify_strips_planted_unsupported_block() {
        let text = "## Purpose\n\nGrounded claim.\n\nHallucinated claim.";
        // Verifier flags block 3 (the planted hallucination).
        let mut verifier = |_prompt: &str, _system: &str| Some("[3]".to_string());
        let mut verify = Some::<&mut TextVerifier<'_>>(&mut verifier);
        match verify_and_strip(&mut verify, text, &sources()) {
            VerifyOutcome::Verified { text, degraded } => {
                assert!(degraded, "stripping a block degrades the page");
                assert!(text.contains("Grounded claim."), "{text}");
                assert!(!text.contains("Hallucinated claim."), "{text}");
            }
            _ => panic!("expected Verified with a stripped block"),
        }
    }

    #[test]
    fn verify_keeps_fully_supported_page_undegraded() {
        let text = "## Purpose\n\nGrounded claim.";
        let mut verifier = |_prompt: &str, _system: &str| Some("[]".to_string());
        let mut verify = Some::<&mut TextVerifier<'_>>(&mut verifier);
        match verify_and_strip(&mut verify, text, &sources()) {
            VerifyOutcome::Verified {
                text: out,
                degraded,
            } => {
                assert!(!degraded, "no strip means no degradation");
                assert_eq!(out, text);
            }
            _ => panic!("expected Verified, undegraded"),
        }
    }

    #[test]
    fn verify_skips_when_verifier_unavailable() {
        let text = "## Purpose\n\nGrounded claim.";
        // Closure returns None: verifier ran but the model call failed.
        let mut verifier = |_prompt: &str, _system: &str| None;
        let mut verify = Some::<&mut TextVerifier<'_>>(&mut verifier);
        assert!(matches!(
            verify_and_strip(&mut verify, text, &sources()),
            VerifyOutcome::Skipped
        ));
    }

    #[test]
    fn verify_skips_when_no_verifier_configured() {
        let text = "## Purpose\n\nGrounded claim.";
        let mut verify: Option<&mut TextVerifier<'_>> = None;
        assert!(matches!(
            verify_and_strip(&mut verify, text, &sources()),
            VerifyOutcome::Skipped
        ));
    }

    #[test]
    fn verify_reports_garbage_verdict_as_unusable() {
        let text = "## Purpose\n\nGrounded claim.";
        let mut verifier = |_prompt: &str, _system: &str| Some("the page looks fine".to_string());
        let mut verify = Some::<&mut TextVerifier<'_>>(&mut verifier);
        assert!(matches!(
            verify_and_strip(&mut verify, text, &sources()),
            VerifyOutcome::Unusable
        ));
    }

    #[test]
    fn verify_reports_total_strip_as_unusable() {
        let text = "Only claim one.\n\nOnly claim two.";
        // Every block flagged -> nothing survives.
        let mut verifier = |_prompt: &str, _system: &str| Some("[1, 2]".to_string());
        let mut verify = Some::<&mut TextVerifier<'_>>(&mut verifier);
        assert!(matches!(
            verify_and_strip(&mut verify, text, &sources()),
            VerifyOutcome::Unusable
        ));
    }
}

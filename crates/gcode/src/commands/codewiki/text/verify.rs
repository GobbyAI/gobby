//! Grounded verification pass for generated codewiki narrative.
//!
//! Generation can drift from the cited source. This module runs an optional
//! second pass over a generated page body: split the prose into numbered
//! blocks, ask the verifier which blocks are unsupported by the cited source
//! excerpts, and keep those findings as frontmatter-only notes. The verifier
//! never rewrites prose, so it cannot launder a hallucination into the page.
//! The deterministic block split and response parse live here (no AI
//! dependency) so they are unit-testable, and the
//! [`super::resolve_text_verifier`] closure is just the model call.

use serde::Deserialize;

use crate::commands::codewiki::prompts::{self, SourceExcerpt, SymbolSummary};
use crate::commands::codewiki::{RelationshipFacts, TextVerifier, VerifyNote};

/// Outcome of one verification pass over generated prose.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum VerifyOutcome {
    /// No verifier ran (not configured, unavailable, or nothing to verify):
    /// proceed with the original text, undegraded.
    Skipped,
    /// The verifier ran and the page survives. `text` is the full original
    /// prose; `notes` is the frontmatter-only audit trail for unsupported
    /// blocks.
    Verified {
        text: String,
        notes: Vec<VerifyNote>,
    },
}

/// Run the optional verification pass over `text`, grounding it against
/// `sources`. Failure-rule mapping:
/// - no verifier / verifier unavailable / nothing to verify -> [`VerifyOutcome::Skipped`]
/// - all blocks supported -> `Verified { notes: [] }`
/// - unsupported blocks -> `Verified { notes }` with the original text intact
/// - malformed verdict -> [`VerifyOutcome::Skipped`]
pub(crate) fn verify_with_notes(
    verify: &mut Option<&mut TextVerifier<'_>>,
    text: &str,
    symbols: &[SymbolSummary],
    sources: &[SourceExcerpt],
    relationships: &RelationshipFacts,
) -> VerifyOutcome {
    let Some(verify) = verify.as_deref_mut() else {
        return VerifyOutcome::Skipped;
    };
    let blocks = split_blocks(text);
    if blocks.is_empty() {
        return VerifyOutcome::Skipped;
    }
    let prompt = prompts::verify_prompt(&blocks, symbols, sources, relationships);
    let Some(response) = verify(&prompt, prompts::VERIFY_SYSTEM) else {
        // Verifier unavailable (routed off / transport failure): skip.
        return VerifyOutcome::Skipped;
    };
    let Some(notes) = parse_verify_notes(&response, blocks.len()) else {
        // A malformed verifier response should not make a generated page worse.
        return VerifyOutcome::Skipped;
    };
    VerifyOutcome::Verified {
        text: text.to_string(),
        notes,
    }
}

/// Split generated markdown into blank-line-separated blocks. Each block is the
/// unit the verifier numbers, so this split and the numbering in
/// [`prompts::verify_prompt`] must stay in lockstep.
pub(crate) fn split_blocks(text: &str) -> Vec<String> {
    text.split("\n\n")
        .map(str::trim)
        .filter(|block| !block.is_empty())
        .map(str::to_string)
        .collect()
}

#[derive(Deserialize)]
struct VerifyNoteResponse {
    id: usize,
    reason: String,
}

/// Parse the verifier's JSON-array response into 1-based unsupported-block
/// notes. Returns `None` when no object array is present, and `Some(vec![])`
/// when every block is supported. Out-of-range and duplicate IDs are dropped.
pub(crate) fn parse_verify_notes(response: &str, block_count: usize) -> Option<Vec<VerifyNote>> {
    let notes = serde_json::from_str::<Vec<VerifyNoteResponse>>(response.trim()).ok()?;
    let mut filtered = notes
        .into_iter()
        .filter(|note| (1..=block_count).contains(&note.id))
        .map(|note| VerifyNote::new(note.id, note.reason))
        .collect::<Vec<_>>();
    filtered.sort_by_key(|note| note.id);
    filtered.dedup_by_key(|note| note.id);
    Some(filtered)
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

    fn symbols() -> Vec<SymbolSummary> {
        vec![SymbolSummary {
            name: "run|cli".to_string(),
            kind: "function".to_string(),
            component_id: "component|id".to_string(),
            component_label: "run [function]".to_string(),
            line_start: 7,
            line_end: 9,
            purpose: "Handles command dispatch.".to_string(),
        }]
    }

    #[test]
    fn split_blocks_separates_on_blank_lines() {
        let blocks = split_blocks("## Purpose\n\nFirst para.\n\nSecond para.\n");
        assert_eq!(blocks, ["## Purpose", "First para.", "Second para."]);
    }

    #[test]
    fn parse_verify_notes_reads_json_array() {
        assert_eq!(
            parse_verify_notes(
                r#"[{"id":2,"reason":" unsupported "},{"id":3,"reason":"Invented behavior."}]"#,
                5,
            ),
            Some(vec![
                VerifyNote::new(2, "unsupported"),
                VerifyNote::new(3, "Invented behavior.")
            ])
        );
        assert_eq!(parse_verify_notes("[]", 5), Some(vec![]));
        // Out-of-range and duplicate IDs are dropped.
        assert_eq!(
            parse_verify_notes(
                r#"[{"id":0,"reason":"bad"},{"id":2,"reason":"kept"},{"id":2,"reason":"duplicate"},{"id":9,"reason":"bad"}]"#,
                3,
            ),
            Some(vec![VerifyNote::new(2, "kept")])
        );
    }

    #[test]
    fn parse_verify_notes_rejects_garbage() {
        assert_eq!(parse_verify_notes("all good", 5), None);
        assert_eq!(parse_verify_notes("", 5), None);
        assert_eq!(
            parse_verify_notes(r#"[{"id":1,"reason":"missing close"}"#, 5),
            None
        );
        assert_eq!(parse_verify_notes("[1, 2]", 5), None);
    }

    #[test]
    fn parse_verify_notes_trims_and_caps_reason() {
        let long = format!(" {} ", "x".repeat(250));
        let note = VerifyNote::new(1, long);
        assert_eq!(note.reason.len(), 200);
        assert!(note.reason.ends_with("..."));
    }

    #[test]
    fn verify_records_planted_unsupported_block_without_stripping() {
        let text = "## Purpose\n\nGrounded claim.\n\nHallucinated claim.";
        let mut verifier = |_prompt: &str, _system: &str| {
            Some(r#"[{"id":3,"reason":"The evidence never mentions this claim."}]"#.to_string())
        };
        let mut verify = Some::<&mut TextVerifier<'_>>(&mut verifier);
        match verify_with_notes(
            &mut verify,
            text,
            &[],
            &sources(),
            &RelationshipFacts::default(),
        ) {
            VerifyOutcome::Verified { text: out, notes } => {
                assert_eq!(out, text);
                assert!(out.contains("Grounded claim."), "{out}");
                assert!(out.contains("Hallucinated claim."), "{out}");
                assert_eq!(
                    notes,
                    vec![VerifyNote::new(
                        3,
                        "The evidence never mentions this claim."
                    )]
                );
            }
            _ => panic!("expected Verified with an audit note"),
        }
    }

    #[test]
    fn verify_keeps_fully_supported_page_without_notes() {
        let text = "## Purpose\n\nGrounded claim.";
        let mut verifier = |_prompt: &str, _system: &str| Some("[]".to_string());
        let mut verify = Some::<&mut TextVerifier<'_>>(&mut verifier);
        match verify_with_notes(
            &mut verify,
            text,
            &[],
            &sources(),
            &RelationshipFacts::default(),
        ) {
            VerifyOutcome::Verified { text: out, notes } => {
                assert_eq!(out, text);
                assert!(notes.is_empty());
            }
            _ => panic!("expected Verified without notes"),
        }
    }

    #[test]
    fn verify_forwards_symbol_evidence_to_prompt() {
        let text = "## Purpose\n\nThe run symbol handles command dispatch.";
        let mut captured_prompt = String::new();
        {
            let mut verifier = |prompt: &str, _system: &str| {
                captured_prompt = prompt.to_string();
                Some("[]".to_string())
            };
            let mut verify = Some::<&mut TextVerifier<'_>>(&mut verifier);
            assert!(matches!(
                verify_with_notes(
                    &mut verify,
                    text,
                    &symbols(),
                    &sources(),
                    &RelationshipFacts::default()
                ),
                VerifyOutcome::Verified { text: _, notes: _ }
            ));
        }

        assert!(captured_prompt.contains("Symbols:\n"), "{captured_prompt}");
        assert!(
            captured_prompt.contains(
                "| run\\|cli | function | run [function] | component\\|id | 7-9 | Handles command dispatch. |"
            ),
            "{captured_prompt}"
        );
    }

    #[test]
    fn verify_skips_when_verifier_unavailable() {
        let text = "## Purpose\n\nGrounded claim.";
        // Closure returns None: verifier ran but the model call failed.
        let mut verifier = |_prompt: &str, _system: &str| None;
        let mut verify = Some::<&mut TextVerifier<'_>>(&mut verifier);
        assert!(matches!(
            verify_with_notes(
                &mut verify,
                text,
                &[],
                &sources(),
                &RelationshipFacts::default()
            ),
            VerifyOutcome::Skipped
        ));
    }

    #[test]
    fn verify_skips_when_no_verifier_configured() {
        let text = "## Purpose\n\nGrounded claim.";
        let mut verify: Option<&mut TextVerifier<'_>> = None;
        assert!(matches!(
            verify_with_notes(
                &mut verify,
                text,
                &[],
                &sources(),
                &RelationshipFacts::default()
            ),
            VerifyOutcome::Skipped
        ));
    }

    #[test]
    fn verify_skips_garbage_verdict() {
        let text = "## Purpose\n\nGrounded claim.";
        let mut verifier = |_prompt: &str, _system: &str| Some("the page looks fine".to_string());
        let mut verify = Some::<&mut TextVerifier<'_>>(&mut verifier);
        assert!(matches!(
            verify_with_notes(
                &mut verify,
                text,
                &[],
                &sources(),
                &RelationshipFacts::default()
            ),
            VerifyOutcome::Skipped
        ));
    }

    #[test]
    fn verify_records_total_flag_as_notes() {
        let text = "Only claim one.\n\nOnly claim two.";
        let mut verifier = |_prompt: &str, _system: &str| {
            Some(
                r#"[{"id":1,"reason":"Unsupported one."},{"id":2,"reason":"Unsupported two."}]"#
                    .to_string(),
            )
        };
        let mut verify = Some::<&mut TextVerifier<'_>>(&mut verifier);
        match verify_with_notes(
            &mut verify,
            text,
            &[],
            &sources(),
            &RelationshipFacts::default(),
        ) {
            VerifyOutcome::Verified { text: out, notes } => {
                assert_eq!(out, text);
                assert_eq!(
                    notes,
                    vec![
                        VerifyNote::new(1, "Unsupported one."),
                        VerifyNote::new(2, "Unsupported two.")
                    ]
                );
            }
            _ => panic!("expected Verified with notes"),
        }
    }
}

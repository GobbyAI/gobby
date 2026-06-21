mod citations;
mod frontmatter;
mod generation;
mod sanitize;
mod structural;
mod verify;

pub(crate) use citations::{
    CitationResolver, citation_list, citation_markers, ground_text, reanchor_citations,
    replace_citations_with_markers, write_references,
};
pub(crate) use frontmatter::{
    append_curated_source_files, append_relevant_source_files, frontmatter_with_degradation,
    frontmatter_with_degradation_and_verify_notes_without_ranges,
    frontmatter_with_degradation_without_ranges,
};
pub(crate) use generation::{
    Generation, maybe_generate, resolve_text_generator, resolve_text_verifier,
};
pub(crate) use sanitize::neutralize_symbol_purpose_links;
pub(crate) use structural::{
    collect_link_spans, display_child_summary, structural_file_summary, structural_module_summary,
    structural_repo_summary, structural_symbol_purpose, write_section,
};
pub(crate) use verify::{VerifyOutcome, verify_with_notes};

#[cfg(test)]
pub(crate) use citations::MAX_FALLBACK_CITATIONS;
#[cfg(test)]
pub(crate) use frontmatter::frontmatter;
#[cfg(test)]
pub(crate) use generation::generate_with_bounded_retry;

#[cfg(test)]
use citations::{fallback_spans, wrap_citation_items};
#[cfg(test)]
use frontmatter::MAX_FRONTMATTER_PROVENANCE_FILES;
#[cfg(test)]
use generation::{GENERATION_RETRY_BACKOFF, is_model_refusal, is_prompt_echo};

#[cfg(test)]
mod tests {
    use super::super::{PromptTier, SourceSpan, TextGenerator, io, prompts};
    use super::{
        GENERATION_RETRY_BACKOFF, Generation, MAX_FALLBACK_CITATIONS,
        MAX_FRONTMATTER_PROVENANCE_FILES, citation_list, citation_markers, fallback_spans,
        frontmatter, generate_with_bounded_retry, is_model_refusal, is_prompt_echo, maybe_generate,
        wrap_citation_items, write_references,
    };
    use gobby_core::ai_types::AiError;

    fn span(file: impl Into<String>, line_start: usize, line_end: usize) -> SourceSpan {
        SourceSpan {
            file: file.into(),
            line_start,
            line_end,
        }
    }

    #[test]
    fn frontmatter_coalesces_contiguous_provenance_ranges() {
        let doc = frontmatter(
            "Repository Overview",
            "code_repo",
            &[
                span("src/lib.rs", 2, 2),
                span("src/lib.rs", 3, 3),
                span("src/lib.rs", 4, 6),
                span("src/lib.rs", 8, 8),
                span("src/lib.rs", 9, 10),
                span("src/lib.rs", 12, 12),
            ],
        );

        assert!(doc.contains("- 2-6"), "{doc}");
        assert!(doc.contains("- 8-10"), "{doc}");
        assert!(doc.contains("- '12'"), "{doc}");
        assert!(!doc.contains("- '3'"), "{doc}");
        assert!(!doc.contains("- '9'"), "{doc}");
    }

    #[test]
    fn citation_list_emits_one_fallback_range_per_line() {
        let spans = (0..3)
            .map(|index| {
                span(
                    format!(
                        "crates/gcode/src/generated/deep/module/path/with/long/components/file_{index}.rs",
                    ),
                    index + 1,
                    index + 10,
                )
            })
            .collect::<Vec<_>>();

        let citations = citation_list(&spans, "");

        let lines = citations.lines().collect::<Vec<_>>();
        assert_eq!(lines.len(), spans.len(), "{citations}");
        for (line, span) in lines.iter().zip(spans) {
            assert_eq!(*line, span.citation());
        }
    }

    #[test]
    fn citation_list_caps_oversized_span_sets() {
        let spans = (0..200)
            .map(|index| span(format!("src/file_{index:03}.rs"), 1, 10))
            .collect::<Vec<_>>();

        let citations = citation_list(&spans, "");

        assert_eq!(
            citations.lines().count(),
            MAX_FALLBACK_CITATIONS,
            "{citations}"
        );
    }

    #[test]
    fn fallback_spans_prefer_distinct_files_over_one_files_span_run() {
        let mut spans = (1..100)
            .map(|line| span("src/big.rs", line, line))
            .collect::<Vec<_>>();
        spans.push(span("src/other.rs", 1, 5));

        let picked = fallback_spans(&spans, "");

        assert!(picked.len() <= MAX_FALLBACK_CITATIONS);
        assert!(
            picked.iter().any(|span| span.file == "src/other.rs"),
            "distinct file must be represented: {picked:?}"
        );
    }

    #[test]
    fn citation_markers_are_capped_and_keep_reference_numbering() {
        let spans = (0..80)
            .map(|index| span(format!("src/file_{index:02}.rs"), 1, 1))
            .collect::<Vec<_>>();

        let markers = citation_markers(&spans, "");

        assert_eq!(
            markers.split_whitespace().count(),
            MAX_FALLBACK_CITATIONS,
            "{markers}"
        );
        assert!(markers.starts_with("[1]"), "{markers}");
    }

    #[test]
    fn fallback_citations_rank_lexically_relevant_files_first() {
        let spans = vec![
            span("src/aardvark.rs", 1, 10),
            span("src/parser.rs", 1, 10),
            span("src/zoo.rs", 1, 10),
        ];

        let picked = fallback_spans(&spans, "The parser walks the AST and emits tokens.");

        assert_eq!(picked[0].file, "src/parser.rs", "{picked:?}");
    }

    #[test]
    fn asset_data_files_rank_behind_source_unless_sole_provenance() {
        let spans = vec![
            span("assets/data.json", 1, 10),
            span("src/zz_late.rs", 1, 10),
        ];
        let picked = fallback_spans(&spans, "");
        assert_eq!(picked[0].file, "src/zz_late.rs", "{picked:?}");

        let sole = vec![span("assets/data.json", 1, 10)];
        let picked = fallback_spans(&sole, "");
        assert_eq!(picked[0].file, "assets/data.json", "{picked:?}");
    }

    #[test]
    fn frontmatter_caps_provenance_and_records_truncation() {
        let mut spans = Vec::new();
        for index in 0..MAX_FRONTMATTER_PROVENANCE_FILES + 7 {
            spans.push(span(format!("src/file_{index:02}.rs"), 1, 5));
        }
        // The busiest file contributes extra spans, so it must survive the cap
        // even though it sorts last alphabetically.
        let busiest = "src/zz_busiest.rs";
        for line in [1, 10, 20, 30] {
            spans.push(span(busiest, line, line + 2));
        }

        let doc = frontmatter("Repository Overview", "code_repo", &spans);

        let kept_files = io::source_files_from_frontmatter(&doc);
        assert_eq!(kept_files.len(), MAX_FRONTMATTER_PROVENANCE_FILES, "{doc}");
        assert!(kept_files.contains(busiest), "{doc}");
        let truncated_marker = format!(
            "{}: 8",
            gobby_core::codewiki_contract::PROVENANCE_TRUNCATED_KEY
        );
        assert!(
            doc.contains(&truncated_marker),
            "7 overflow files + 1 displaced by the busiest file: {doc}"
        );

        let bounded = frontmatter(
            "src/lib.rs",
            "code_file",
            &[span("src/lib.rs", 1, 2), span("src/lib.rs", 9, 9)],
        );
        assert!(!bounded.contains("provenance_truncated"), "{bounded}");
    }

    #[test]
    fn references_resolve_only_markers_present_in_doc() {
        let spans = (0..40)
            .map(|index| span(format!("src/file_{index:02}.rs"), 1, 1))
            .collect::<Vec<_>>();
        let mut doc = "Prose citing [2] and [17] only.\n\n".to_string();

        write_references(&mut doc, &spans);

        let references = doc
            .lines()
            .filter(|line| line.starts_with("- ["))
            .collect::<Vec<_>>();
        assert_eq!(references.len(), 2, "{doc}");
        assert!(references[0].starts_with("- [2] "), "{doc}");
        assert!(references[1].starts_with("- [17] "), "{doc}");
    }

    #[test]
    fn wrap_citation_items_bounds_line_width() {
        let items = (0..80).map(|index| format!("[{index}]"));

        let wrapped = wrap_citation_items(items, 40);

        assert!(wrapped.lines().count() > 1, "{wrapped}");
        assert!(wrapped.lines().all(|line| line.len() <= 40), "{wrapped}");
    }

    #[test]
    fn prompt_echo_is_rejected_as_failed_generation() {
        let prompt = prompts::module_prompt(
            "crates/gcode",
            &[prompts::ChildSummary {
                name: "crates/gcode/Cargo.toml".to_string(),
                summary: "Manifest for the gcode binary.".to_string(),
            }],
            &[],
            &[],
            &[],
            &crate::commands::codewiki::RelationshipFacts::default(),
        );

        let mut echoing = |prompt: &str, _system: &str, _tier: PromptTier| Some(prompt.to_string());
        let mut generate = Some::<&mut TextGenerator<'_>>(&mut echoing);
        let generation = maybe_generate(
            &mut generate,
            &prompt,
            prompts::MODULE_SYSTEM,
            PromptTier::Aggregate,
        );
        assert!(generation.failed(), "prompt echo must record degradation");

        let mut healthy = |_prompt: &str, _system: &str, _tier: PromptTier| {
            Some("`crates/gcode` indexes source and serves search.".to_string())
        };
        let mut generate = Some::<&mut TextGenerator<'_>>(&mut healthy);
        let generation = maybe_generate(
            &mut generate,
            &prompt,
            prompts::MODULE_SYSTEM,
            PromptTier::Aggregate,
        );
        assert!(matches!(generation, Generation::Generated(_)));
    }

    #[test]
    fn short_prompts_never_trigger_echo_rejection() {
        let prompt = "Short prompt.";
        assert!(!is_prompt_echo("Short prompt.", prompt));
    }

    #[test]
    fn model_refusal_is_detected_but_real_prose_is_not() {
        // The actual failure mode (#904): a weak model refused the curated
        // narrative prompt and the apology shipped as the page body.
        let refusal = "# Welcome to Gcode\n\nI cannot write this chapter as specified. \
             The supplied evidence is insufficient to create a guided-tour chapter.";
        assert!(is_model_refusal(refusal));
        // Grounded prose that merely discusses a limitation must not be flagged:
        // "it cannot index" is not a first-person refusal to write the page.
        let prose = "The indexing pipeline parses each file with tree-sitter and writes \
             symbols to PostgreSQL. It cannot index binary files, which are skipped.";
        assert!(!is_model_refusal(prose));
    }

    #[test]
    fn refusal_marker_after_the_lead_is_ignored() {
        // Only the opening is scanned, so a long real body that happens to use
        // the phrase deep in the prose is not misflagged as a refusal.
        let body = format!(
            "{}\n\nA contributor once joked they i cannot write tests fast enough.",
            "Real grounded prose about the parser. ".repeat(30)
        );
        assert!(!is_model_refusal(&body));
    }

    #[test]
    fn refusal_body_makes_maybe_generate_fail_and_fall_back() {
        let mut refusing = |_prompt: &str, _system: &str, _tier: PromptTier| {
            Some("I am unable to write this page.".to_string())
        };
        let mut generate = Some::<&mut TextGenerator<'_>>(&mut refusing);
        let generation = maybe_generate(
            &mut generate,
            "Write the repository overview.",
            prompts::REPO_SYSTEM,
            PromptTier::Aggregate,
        );
        assert!(generation.failed(), "model refusal must record degradation");
    }

    fn transport_failure() -> AiError {
        AiError::TransportFailure {
            status: None,
            body: None,
            source: "connection reset".to_string(),
        }
    }

    #[test]
    fn bounded_retry_recovers_from_transient_transport_failure() {
        let mut calls = 0_usize;
        let result = generate_with_bounded_retry(|| {
            calls += 1;
            if calls == 1 {
                Err(transport_failure())
            } else {
                Ok("generated".to_string())
            }
        });

        assert_eq!(result.expect("retry recovers"), "generated");
        assert_eq!(calls, 2);
    }

    #[test]
    fn bounded_retry_gives_up_after_bounded_attempts() {
        let mut calls = 0_usize;
        let result: Result<String, AiError> = generate_with_bounded_retry(|| {
            calls += 1;
            Err(transport_failure())
        });

        assert!(result.is_err());
        assert_eq!(calls, 1 + GENERATION_RETRY_BACKOFF.len());
    }

    #[test]
    fn bounded_retry_fails_fast_on_non_transient_errors() {
        let mut calls = 0_usize;
        let result: Result<String, AiError> = generate_with_bounded_retry(|| {
            calls += 1;
            Err(AiError::NotConfigured {
                capability: None,
                message: "no provider".to_string(),
            })
        });

        assert!(result.is_err());
        assert_eq!(calls, 1);
    }
}

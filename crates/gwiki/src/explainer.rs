//! Bounded LLM explainer synthesis for compiled wiki articles.
//!
//! `compile` keeps its deterministic skeleton; this module adds the optional
//! prose layer: a single bounded prompt over the accepted sources, a
//! transport-agnostic generation seam, and grounding that validates every
//! citation marker against the accepted sources before prose reaches the
//! vault. Generation failure degrades to the structural skeleton with
//! explicit degradation markers; AI off skips synthesis without markers.

use std::path::Path;

use serde::Serialize;

use crate::synthesis::{SynthesisInput, relative_path};

/// Hard cap on the estimated token count of the explainer prompt (system +
/// user), matching `gwiki ask`'s prompt budget discipline.
pub const EXPLAINER_PROMPT_TOKEN_BUDGET: usize = 12_000;

/// Per-source excerpt bound entering the prompt.
pub const EXPLAINER_SOURCE_EXCERPT_MAX_CHARS: usize = 2_400;

/// Conservative ~4 chars/token estimate, rounded up.
pub fn estimate_tokens(chars: usize) -> usize {
    chars.div_ceil(4)
}

/// System prompt for the explainer completion. Citations use machine-checkable
/// `[source: <path>]` markers so grounding can validate them afterwards.
pub const EXPLAINER_SYSTEM: &str = "You write a grounded wiki explainer from supplied source \
     excerpts. Use only facts stated in the excerpts. Write each requested section as a markdown \
     '## <section>' heading followed by one or two short paragraphs. Plain paragraphs only - no \
     extra headings, no lists, no code fences. Cite supporting sources inline with \
     [source: <path>] markers, copying a path exactly as listed. Never invent paths. If the \
     excerpts do not cover a section, state that the sources do not cover it.";

/// Fully assembled explainer prompt plus budget accounting.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExplainerPrompt {
    pub system: &'static str,
    pub user: String,
    pub tokens_estimated: usize,
    /// Accepted sources dropped because the prompt hit the token budget.
    pub truncated_sources: usize,
}

/// One successful completion from a transport (daemon or direct).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExplainerResponse {
    pub text: String,
    pub model: Option<String>,
    pub route: &'static str,
}

/// Transport seam: the CLI wires daemon/direct gcore transports; tests inject
/// deterministic closures.
pub type ExplainerGenerator<'a> =
    &'a mut dyn FnMut(&ExplainerPrompt) -> Result<ExplainerResponse, String>;

/// Outcome of one explainer attempt, mirroring codewiki's honesty contract:
/// `Failed` means an attempt was made and degrades the page; `Skipped` means
/// synthesis was off by intent and the structural skeleton is not degraded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExplainerGeneration {
    Generated {
        body: String,
        model: Option<String>,
        route: &'static str,
    },
    Failed {
        error: String,
    },
    Skipped,
}

/// Per-article explainer accounting surfaced through the compile payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ExplainerReport {
    pub status: &'static str,
    pub route: Option<&'static str>,
    pub model: Option<String>,
    pub error: Option<String>,
    pub citations_kept: usize,
    pub citations_stripped: usize,
    pub fallback_sections: usize,
}

impl ExplainerReport {
    pub fn skipped() -> Self {
        Self {
            status: "skipped",
            route: None,
            model: None,
            error: None,
            citations_kept: 0,
            citations_stripped: 0,
            fallback_sections: 0,
        }
    }
}

/// A citation the model is allowed to make: the exact path key listed in the
/// prompt, the wiki link a kept citation is rewritten to, and the source text
/// used for lexical fallback ranking.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CitationTarget {
    pub key: String,
    pub link: String,
    pub corpus: String,
}

/// Grounded explainer body plus citation accounting.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GroundedExplainer {
    pub body: String,
    pub citations_kept: usize,
    pub citations_stripped: usize,
    pub fallback_sections: usize,
}

/// Assemble the bounded explainer prompt: topic, requested sections, then
/// accepted sources in order until the next entry would exceed the budget.
pub fn build_explainer_prompt(vault_root: &Path, input: &SynthesisInput) -> ExplainerPrompt {
    let mut user = format!("Topic: {}\n", input.topic);
    user.push_str("Write the explainer with exactly these sections:\n");
    if input.outline.is_empty() {
        user.push_str("- Overview\n");
    } else {
        for heading in &input.outline {
            user.push_str("- ");
            user.push_str(heading);
            user.push('\n');
        }
    }
    user.push_str("\nSource excerpts:\n");

    let system_chars = EXPLAINER_SYSTEM.chars().count();
    let mut truncated_sources = 0usize;
    if input.accepted_sources.is_empty() {
        user.push_str("- No accepted sources.\n");
    }
    for (index, source) in input.accepted_sources.iter().enumerate() {
        let key = relative_path(vault_root, &source.path);
        let excerpt = bounded_excerpt(
            &source.chunks.join("\n"),
            EXPLAINER_SOURCE_EXCERPT_MAX_CHARS,
        );
        let entry = format!(
            "{}. {} [source: {key}]\n{excerpt}\n\n",
            index + 1,
            source.title
        );
        let projected = user.chars().count() + entry.chars().count() + system_chars;
        if estimate_tokens(projected) > EXPLAINER_PROMPT_TOKEN_BUDGET {
            truncated_sources = input.accepted_sources.len() - index;
            break;
        }
        user.push_str(&entry);
    }

    let tokens_estimated = estimate_tokens(user.chars().count() + system_chars);
    ExplainerPrompt {
        system: EXPLAINER_SYSTEM,
        user,
        tokens_estimated,
        truncated_sources,
    }
}

/// Run one explainer attempt through the injected generator. No generator or
/// no accepted sources means synthesis is structurally off, not degraded.
pub fn generate_explainer(
    input: &SynthesisInput,
    prompt: &ExplainerPrompt,
    generator: Option<ExplainerGenerator<'_>>,
) -> ExplainerGeneration {
    let Some(generator) = generator else {
        return ExplainerGeneration::Skipped;
    };
    if input.accepted_sources.is_empty() {
        return ExplainerGeneration::Skipped;
    }
    match generator(prompt) {
        Ok(response) => {
            let body = response.text.trim().to_string();
            if body.is_empty() {
                ExplainerGeneration::Failed {
                    error: "model returned an empty explainer".to_string(),
                }
            } else {
                ExplainerGeneration::Generated {
                    body,
                    model: response.model,
                    route: response.route,
                }
            }
        }
        Err(error) => ExplainerGeneration::Failed { error },
    }
}

/// Validate generated citations against the accepted sources: a marker whose
/// path matches a target is rewritten to that target's wiki link; any other
/// marker is stripped. Sections left with prose but no surviving citation get
/// a fallback citation to the lexically closest source.
pub fn ground_explainer(body: &str, targets: &[CitationTarget]) -> GroundedExplainer {
    let mut citations_kept = 0usize;
    let mut citations_stripped = 0usize;
    let mut grounded = String::with_capacity(body.len());
    let mut rest = body;

    while let Some(start) = rest.find("[source:") {
        let (before, marker_on) = rest.split_at(start);
        let Some(end) = marker_on.find(']') else {
            grounded.push_str(before);
            grounded.push_str(marker_on);
            rest = "";
            break;
        };
        let key = marker_on["[source:".len()..end].trim();
        match targets
            .iter()
            .find(|target| citation_key_matches(&target.key, key))
        {
            Some(target) => {
                citations_kept += 1;
                grounded.push_str(before);
                grounded.push_str(&target.link);
            }
            None => {
                citations_stripped += 1;
                grounded.push_str(before.strip_suffix(' ').unwrap_or(before));
            }
        }
        rest = &marker_on[end + 1..];
    }
    grounded.push_str(rest);

    let (body, fallback_sections) = apply_section_fallbacks(&grounded, targets);
    GroundedExplainer {
        body,
        citations_kept,
        citations_stripped,
        fallback_sections,
    }
}

fn citation_key_matches(target_key: &str, cited: &str) -> bool {
    target_key == cited
        || target_key
            .strip_suffix(".md")
            .is_some_and(|trimmed| trimmed == cited)
}

/// Append a fallback citation to each `##` section (and the preamble) that
/// has prose but no surviving citation link.
fn apply_section_fallbacks(body: &str, targets: &[CitationTarget]) -> (String, usize) {
    if targets.is_empty() {
        return (body.to_string(), 0);
    }
    let mut sections: Vec<String> = Vec::new();
    for line in body.lines() {
        if line.starts_with("## ") || sections.is_empty() {
            sections.push(String::new());
        }
        let section = sections.last_mut().expect("section pushed above");
        section.push_str(line);
        section.push('\n');
    }

    let mut fallback_sections = 0usize;
    let mut out = String::new();
    for section in &sections {
        let prose: String = section
            .lines()
            .filter(|line| !line.starts_with("## "))
            .collect::<Vec<_>>()
            .join(" ");
        let has_citation = targets.iter().any(|target| section.contains(&target.link));
        out.push_str(section);
        if !prose.trim().is_empty()
            && !has_citation
            && let Some(target) = best_fallback_target(&prose, targets)
        {
            fallback_sections += 1;
            if !out.ends_with("\n\n") {
                out.push('\n');
            }
            out.push_str(&format!("_Source: {}_\n", target.link));
        }
    }
    (out, fallback_sections)
}

/// Rank fallback targets by significant-token overlap with the section prose;
/// ties keep accepted-source order.
fn best_fallback_target<'a>(
    prose: &str,
    targets: &'a [CitationTarget],
) -> Option<&'a CitationTarget> {
    let prose_tokens: std::collections::BTreeSet<String> = significant_tokens(prose);
    targets
        .iter()
        .enumerate()
        .max_by_key(|(index, target)| {
            let overlap = significant_tokens(&target.corpus)
                .intersection(&prose_tokens)
                .count();
            (overlap, std::cmp::Reverse(*index))
        })
        .map(|(_, target)| target)
}

fn significant_tokens(text: &str) -> std::collections::BTreeSet<String> {
    text.split(|ch: char| !ch.is_alphanumeric())
        .filter(|token| token.chars().count() >= 3)
        .map(|token| token.to_lowercase())
        .collect()
}

/// Char-boundary-safe truncation that preserves newlines and marks elision.
fn bounded_excerpt(text: &str, max_chars: usize) -> String {
    let trimmed = text.trim();
    if trimmed.chars().count() <= max_chars {
        return trimmed.to_string();
    }
    let mut excerpt: String = trimmed.chars().take(max_chars).collect();
    excerpt.push('…');
    excerpt
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;
    use crate::synthesis::{ArticleKind, SynthesisSource};

    fn input_with_sources(sources: Vec<SynthesisSource>) -> SynthesisInput {
        SynthesisInput {
            handoff_id: "handoff-1".to_string(),
            topic: "Compile Behavior".to_string(),
            outline: vec!["Overview".to_string(), "Evidence".to_string()],
            target_kind: ArticleKind::Topic,
            accepted_sources: sources,
            citations: vec![],
            conflicting_claims: vec![],
            missing_evidence: vec![],
        }
    }

    fn source(title: &str, path: &str, chunk: &str) -> SynthesisSource {
        SynthesisSource {
            title: title.to_string(),
            path: PathBuf::from(path),
            chunks: vec![chunk.to_string()],
        }
    }

    #[test]
    fn explainer_prompt_lists_sections_and_cited_source_paths() {
        let input = input_with_sources(vec![source(
            "Compile Notes",
            "raw/research/compile.md",
            "Compile turns accepted notes into grounded articles.",
        )]);
        let prompt = build_explainer_prompt(Path::new("/vault"), &input);

        assert_eq!(prompt.system, EXPLAINER_SYSTEM);
        assert!(prompt.user.contains("Topic: Compile Behavior"));
        assert!(prompt.user.contains("- Overview\n- Evidence"));
        assert!(
            prompt
                .user
                .contains("1. Compile Notes [source: raw/research/compile.md]")
        );
        assert!(prompt.user.contains("Compile turns accepted notes"));
        assert!(prompt.tokens_estimated > 0);
        assert_eq!(prompt.truncated_sources, 0);
    }

    #[test]
    fn explainer_prompt_without_outline_requests_an_overview_section() {
        let mut input = input_with_sources(vec![]);
        input.outline.clear();
        let prompt = build_explainer_prompt(Path::new("/vault"), &input);

        assert!(prompt.user.contains("- Overview\n"));
        assert!(prompt.user.contains("- No accepted sources."));
    }

    #[test]
    fn explainer_prompt_never_exceeds_token_budget() {
        let big_chunk = "evidence ".repeat(1_000);
        let sources = (0..40)
            .map(|index| {
                source(
                    &format!("Source {index}"),
                    &format!("raw/{index}.md"),
                    &big_chunk,
                )
            })
            .collect();
        let prompt = build_explainer_prompt(Path::new("/vault"), &input_with_sources(sources));

        assert!(prompt.tokens_estimated <= EXPLAINER_PROMPT_TOKEN_BUDGET);
        assert!(prompt.truncated_sources > 0);
    }

    #[test]
    fn explainer_prompt_bounds_each_source_excerpt() {
        let oversized = "x".repeat(EXPLAINER_SOURCE_EXCERPT_MAX_CHARS + 500);
        let prompt = build_explainer_prompt(
            Path::new("/vault"),
            &input_with_sources(vec![source("Big", "raw/big.md", &oversized)]),
        );

        let excerpt_line = prompt
            .user
            .lines()
            .find(|line| line.starts_with('x'))
            .expect("excerpt line");
        assert_eq!(
            excerpt_line.chars().count(),
            EXPLAINER_SOURCE_EXCERPT_MAX_CHARS + 1
        );
        assert!(excerpt_line.ends_with('…'));
    }

    #[test]
    fn grounding_rewrites_valid_markers_and_strips_invented_ones() {
        let targets = vec![CitationTarget {
            key: "raw/research/compile.md".to_string(),
            link: "[[knowledge/sources/compile-notes|Compile Notes]]".to_string(),
            corpus: "Compile Notes compile turns accepted notes".to_string(),
        }];
        let body = "## Overview\nCompile is grounded [source: raw/research/compile.md].\n\
                    It never invents sources [source: raw/research/madeup.md].\n";

        let grounded = ground_explainer(body, &targets);

        assert!(
            grounded
                .body
                .contains("grounded [[knowledge/sources/compile-notes|Compile Notes]].")
        );
        assert!(!grounded.body.contains("[source:"));
        assert!(!grounded.body.contains("madeup"));
        assert_eq!(grounded.citations_kept, 1);
        assert_eq!(grounded.citations_stripped, 1);
        assert_eq!(grounded.fallback_sections, 0);
    }

    #[test]
    fn grounding_appends_fallback_citation_to_uncited_sections() {
        let targets = vec![
            CitationTarget {
                key: "raw/a.md".to_string(),
                link: "[[knowledge/sources/alpha|Alpha]]".to_string(),
                corpus: "alpha indexing pipeline walker".to_string(),
            },
            CitationTarget {
                key: "raw/b.md".to_string(),
                link: "[[knowledge/sources/beta|Beta]]".to_string(),
                corpus: "beta search ranking fusion".to_string(),
            },
        ];
        let body = "## Overview\nThe search ranking fusion merges results.\n";

        let grounded = ground_explainer(body, &targets);

        assert_eq!(grounded.fallback_sections, 1);
        assert!(
            grounded
                .body
                .contains("_Source: [[knowledge/sources/beta|Beta]]_")
        );
    }

    #[test]
    fn generation_without_sources_or_generator_is_skipped_not_degraded() {
        let input = input_with_sources(vec![]);
        let prompt = build_explainer_prompt(Path::new("/vault"), &input);
        let mut generator = |_prompt: &ExplainerPrompt| -> Result<ExplainerResponse, String> {
            panic!("generator must not run without accepted sources");
        };

        assert_eq!(
            generate_explainer(&input, &prompt, Some(&mut generator)),
            ExplainerGeneration::Skipped
        );
        assert_eq!(
            generate_explainer(&input, &prompt, None),
            ExplainerGeneration::Skipped
        );
    }

    #[test]
    fn generation_failure_and_empty_output_mark_failed() {
        let input = input_with_sources(vec![source("A", "raw/a.md", "alpha")]);
        let prompt = build_explainer_prompt(Path::new("/vault"), &input);

        let mut failing = |_prompt: &ExplainerPrompt| Err("transport down".to_string());
        assert_eq!(
            generate_explainer(&input, &prompt, Some(&mut failing)),
            ExplainerGeneration::Failed {
                error: "transport down".to_string()
            }
        );

        let mut empty = |_prompt: &ExplainerPrompt| {
            Ok(ExplainerResponse {
                text: "  \n".to_string(),
                model: None,
                route: "direct",
            })
        };
        assert!(matches!(
            generate_explainer(&input, &prompt, Some(&mut empty)),
            ExplainerGeneration::Failed { .. }
        ));
    }

    #[test]
    fn generation_success_trims_and_carries_route_and_model() {
        let input = input_with_sources(vec![source("A", "raw/a.md", "alpha")]);
        let prompt = build_explainer_prompt(Path::new("/vault"), &input);
        let mut generator = |_prompt: &ExplainerPrompt| {
            Ok(ExplainerResponse {
                text: "\n## Overview\nAlpha grounds compile [source: raw/a.md].\n".to_string(),
                model: Some("gemma".to_string()),
                route: "daemon",
            })
        };

        let generation = generate_explainer(&input, &prompt, Some(&mut generator));
        match generation {
            ExplainerGeneration::Generated { body, model, route } => {
                assert!(body.starts_with("## Overview"));
                assert_eq!(model.as_deref(), Some("gemma"));
                assert_eq!(route, "daemon");
            }
            other => panic!("expected generated explainer, got {other:?}"),
        }
    }
}

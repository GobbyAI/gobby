use std::fmt::Write as _;

use crate::models::Symbol;

use super::{write_markdown_table_header, write_markdown_table_row};

pub const SYMBOL_SYSTEM: &str = "You write concise API reference notes. Return one sentence describing the symbol's purpose. Do not include markdown fences.";
pub const FILE_SYSTEM: &str = "You write concise file-level code documentation. Return a short purpose summary grounded only in the supplied symbol summaries and source excerpt: what the file does and how its pieces work together. Do not include markdown fences.";
pub const CONTENT_FILE_SYSTEM: &str = "You write concise documentation for non-code repository files. Return a short purpose summary describing what the file contains and what it is for, grounded only in the supplied leading content. Do not include markdown fences.";
pub const MODULE_SYSTEM: &str = "You write module documentation briefs. Using only the supplied file summaries, child module summaries, component table, and source excerpts, write one to two short paragraphs covering the module's responsibilities, key flows, and collaboration points. Add compact Markdown tables for enumerable facts such as CLI commands or flags, configuration keys, environment variables, and public API symbols. No markdown fences. Cite supporting file:line spans that appear in the supplied input.";
pub const REPO_SYSTEM: &str = "You write repository overview briefs. Using only the supplied module summaries, root-file summaries, and source excerpts, write one to two short paragraphs covering what the system is, how the major pieces fit together, and where a reader should start. Add compact Markdown tables for enumerable facts such as CLI commands or flags, configuration keys, environment variables, and public API symbols. No markdown fences. Cite supporting file:line spans that appear in the supplied input.";
pub const ARCHITECTURE_SYSTEM: &str = "You write concise architecture documentation. Using only the supplied summaries, component table, and source excerpts, return a short responsibility summary plus compact Markdown tables for enumerable facts such as public API symbols, CLI commands or flags, configuration keys, and environment variables. No markdown fences.";
pub const ARCHITECTURE_NARRATIVE_SYSTEM: &str = "You write architecture overviews. Using only the supplied subsystem responsibilities and dependency edges, write two to three short paragraphs describing the system in layers: which subsystems sit at the foundation, which build on them, and how the layers interact. Plain paragraphs only - no headings, no lists, no markdown fences.";
pub const CURATED_NAVIGATION_SYSTEM: &str = "You design a curated navigation layer for grounded code documentation. Return strict JSON only. Name user-facing concept modules, organize them into a hierarchy, and create short narrative tour pages. Use only supplied module and file identifiers, and link into reference pages instead of duplicating source detail. Order narrative_pages as a learning path: foundational subsystems first, then the layers that build on them, so the tour reads from chapter one onward.";
pub const CONCEPT_PAGE_SYSTEM: &str = "You write a reference explainer page for one concept in a codebase, written for an engineer who is new to it. Using only the supplied member modules/files, key symbols, and source excerpts, write a multi-section Markdown page with these sections, in order: '## Purpose' (what this concept is and the problem it solves), '## Covers / Does not cover' (the scope boundaries), '## Architecture' (how the pieces fit together; a diagram is injected separately, so describe the structure in prose), '## Data flow' (a numbered list tracing the real runtime flow), '## Key components' (a compact Markdown table of the most important symbols and their role), and '## Where to start' (which page or symbol to read first). Use headings, tables, and lists. Cite supporting file:line anchors that appear in the supplied input. Do not invent files, symbols, or line numbers. No markdown fences.";
pub const NARRATIVE_PAGE_SYSTEM: &str = "You write one chapter of a guided, beginner-friendly tour of a codebase, in the style of a progressive tutorial. Using only the supplied member modules/files, key symbols, and source excerpts, write a multi-section Markdown chapter with these sections, in order: '## Why this matters' (the motivation and the problem this part of the system solves), '## How it works' (a numbered, step-by-step walkthrough of the real flow, grounded in the supplied symbols), '## Key components' (a compact Markdown table of the important symbols), and '## What to read next' (which chapter or reference page to read next). You may include at most one brief analogy if it is anchored to the supplied source; do not pad with long generic metaphors. Use headings, tables, and lists. Cite supporting file:line anchors that appear in the supplied input. Do not invent files, symbols, or line numbers. No markdown fences.";
pub const VERIFY_SYSTEM: &str = "You are a strict citation auditor for code documentation. You receive a draft explanation split into numbered blocks and the source excerpts the page is allowed to rely on. For each block, decide whether its specific technical claims (names, behaviors, control flow, data flow, relationships) are supported by the supplied source excerpts. A block is UNSUPPORTED when it states a concrete technical claim that the excerpts do not show, contradict, or that invents files, symbols, line numbers, or behavior. Treat section headings, navigational sentences, and generic framing as supported. Return ONLY a JSON array of the integer IDs of the unsupported blocks, e.g. [2,5]; return [] when every block is supported. Output nothing but the JSON array: no prose, no explanation, no code fences. Never rewrite the blocks.";

pub fn symbol_prompt(symbol: &Symbol) -> String {
    let mut prompt = format!(
        "File: {}\nSymbol: {} [{}]\nLines: {}-{}",
        symbol.file_path, symbol.qualified_name, symbol.kind, symbol.line_start, symbol.line_end
    );
    if let Some(signature) = symbol
        .signature
        .as_deref()
        .filter(|value| !value.is_empty())
    {
        prompt.push_str("\nSignature: ");
        prompt.push_str(signature);
    }
    if let Some(docstring) = symbol
        .docstring
        .as_deref()
        .filter(|value| !value.is_empty())
    {
        prompt.push_str("\nExisting docs: ");
        prompt.push_str(docstring);
    }
    prompt
}

pub fn file_prompt(file: &str, symbols: &[SymbolSummary], sources: &[SourceExcerpt]) -> String {
    let mut prompt =
        format!("Summarize this file once from its AST symbols.\n\nFile: {file}\n\nSymbols:\n");
    if symbols.is_empty() {
        prompt.push_str("- No indexed symbols.\n");
    } else {
        write_markdown_table_header(
            &mut prompt,
            &[
                "Symbol",
                "Kind",
                "Component",
                "Component ID",
                "Lines",
                "Purpose",
            ],
        );
        for symbol in symbols {
            write_markdown_table_row(
                &mut prompt,
                [
                    symbol.name.clone(),
                    symbol.kind.clone(),
                    symbol.component_label.clone(),
                    symbol.component_id.clone(),
                    format!("{}-{}", symbol.line_start, symbol.line_end),
                    symbol.purpose.clone(),
                ],
            );
        }
    }
    append_source_excerpt_section(&mut prompt, sources);
    prompt
}

/// Prompt for files without indexed AST symbols (markdown, config, data):
/// the model derives a purpose from the file's leading content instead of
/// an empty symbol list.
pub fn content_file_prompt(file: &str, source: &SourceExcerpt) -> String {
    let mut prompt =
        format!("Summarize this repository file once from its leading content.\n\nFile: {file}\n");
    append_source_excerpt_section(&mut prompt, std::slice::from_ref(source));
    prompt
}

pub fn module_prompt(
    module: &str,
    files: &[ChildSummary],
    modules: &[ChildSummary],
    components: &[String],
    sources: &[SourceExcerpt],
) -> String {
    build_entity_prompt(
        "Write a documentation brief for this module from its file summaries, child module summaries, and source excerpts.",
        "Module",
        module,
        files,
        modules,
        components,
        sources,
    )
}

pub fn repo_prompt(
    modules: &[ChildSummary],
    files: &[ChildSummary],
    sources: &[SourceExcerpt],
) -> String {
    let mut prompt =
        "Write a repository overview brief from module summaries, root-file summaries, and source excerpts.\n\n"
            .to_string();
    append_table_guidance(&mut prompt);
    prompt.push_str("Modules:\n");
    if modules.is_empty() {
        prompt.push_str("- No modules.\n");
    } else {
        append_child_summary_table(&mut prompt, &["Module", "Summary"], modules);
    }
    prompt.push_str("\nRoot files:\n");
    if files.is_empty() {
        prompt.push_str("- No root files.\n");
    } else {
        append_child_summary_table(&mut prompt, &["File", "Summary"], files);
    }
    append_source_excerpt_section(&mut prompt, sources);
    prompt
}

fn append_child_summary_table(prompt: &mut String, headers: &[&str], children: &[ChildSummary]) {
    write_markdown_table_header(prompt, headers);
    for child in children {
        write_markdown_table_row(
            prompt,
            [child.name.clone(), summary_excerpt(&child.summary)],
        );
    }
}

fn append_component_table(prompt: &mut String, components: &[String]) {
    write_markdown_table_header(prompt, &["Component"]);
    for component in components {
        write_markdown_table_row(prompt, [component.clone()]);
    }
}

fn append_table_guidance(prompt: &mut String) {
    prompt.push_str("Table guidance:\n");
    prompt.push_str(ENUMERABLE_FACTS_GUIDANCE);
    prompt.push_str("\n\n");
}

pub fn architecture_prompt(
    subsystem: &str,
    files: &[ChildSummary],
    modules: &[ChildSummary],
    components: &[String],
    sources: &[SourceExcerpt],
) -> String {
    build_entity_prompt(
        "Summarize this subsystem's responsibility for a repository architecture overview.",
        "Subsystem",
        subsystem,
        files,
        modules,
        components,
        sources,
    )
}

/// Prompt for the architecture page's layered narrative: subsystem
/// responsibilities plus the cross-subsystem dependency edges.
pub fn architecture_narrative_prompt(
    subsystems: &[ChildSummary],
    edges: &[(String, String)],
) -> String {
    let mut prompt =
        "Write a layered architecture narrative for this repository from its subsystem responsibilities and dependency edges.\n\nSubsystems:\n"
            .to_string();
    if subsystems.is_empty() {
        prompt.push_str("- No subsystems.\n");
    } else {
        for subsystem in subsystems {
            let _ = writeln!(
                prompt,
                "- {}: {}",
                subsystem.name,
                summary_excerpt(&subsystem.summary)
            );
        }
    }
    prompt.push_str("\nDependency edges (source depends on target):\n");
    if edges.is_empty() {
        prompt.push_str("- No cross-subsystem dependency edges.\n");
    } else {
        for (source, target) in edges {
            let _ = writeln!(prompt, "- {source} -> {target}");
        }
    }
    prompt
}

#[allow(clippy::too_many_arguments)]
/// One source-grounded row for a curated page content prompt: a member
/// module/file or a key symbol paired with a `file:line` citation.
/// [`ChildSummary`] alone carries no location, so the content pass would have
/// nothing real to cite; these rows give the model concrete anchors to ground
/// prose against (review #4).
pub struct PageEvidenceRow {
    pub name: String,
    pub kind: String,
    pub citation: String,
    pub summary: String,
}

/// Build the per-page content prompt for a curated concept page (reference
/// explainer voice; pair with [`CONCEPT_PAGE_SYSTEM`]).
pub fn concept_page_prompt(
    title: &str,
    summary: &str,
    members: &[PageEvidenceRow],
    symbols: &[PageEvidenceRow],
    sources: &[SourceExcerpt],
) -> String {
    build_curated_page_prompt(
        "Write a reference explainer page for this concept.",
        title,
        summary,
        members,
        symbols,
        sources,
        CONCEPT_PAGE_SOURCE_EXCERPTS,
    )
}

/// Build the per-page content prompt for a curated narrative chapter (guided
/// tutorial voice; pair with [`NARRATIVE_PAGE_SYSTEM`]).
pub fn narrative_page_prompt(
    title: &str,
    summary: &str,
    members: &[PageEvidenceRow],
    symbols: &[PageEvidenceRow],
    sources: &[SourceExcerpt],
) -> String {
    build_curated_page_prompt(
        "Write one guided-tour chapter for this part of the codebase.",
        title,
        summary,
        members,
        symbols,
        sources,
        NARRATIVE_PAGE_SOURCE_EXCERPTS,
    )
}

/// Build the verification prompt for a generated curated page: the draft prose
/// split into numbered blocks, followed by the cited source excerpts that are
/// the only evidence the auditor may rely on (pair with [`VERIFY_SYSTEM`]). The
/// block IDs here are the 1-based positions the verifier returns and the caller
/// strips, so numbering must stay in lockstep with the caller's block split.
pub fn verify_prompt(blocks: &[String], sources: &[SourceExcerpt]) -> String {
    let mut prompt = String::from(
        "Audit each numbered draft block against the source excerpts below.\n\nDraft blocks:\n",
    );
    for (index, block) in blocks.iter().enumerate() {
        let _ = writeln!(prompt, "[{}] {}", index + 1, bounded_excerpt(block.trim()));
    }
    append_source_excerpt_section_n(&mut prompt, sources, VERIFY_SOURCE_EXCERPTS);
    prompt
}

fn build_curated_page_prompt(
    header: &str,
    title: &str,
    summary: &str,
    members: &[PageEvidenceRow],
    symbols: &[PageEvidenceRow],
    sources: &[SourceExcerpt],
    excerpt_take: usize,
) -> String {
    let mut prompt = format!(
        "{header}\n\nPage: {title}\n\nWorking summary (expand into full sections; do not just restate it): {}\n\n",
        summary_excerpt(summary)
    );
    append_table_guidance(&mut prompt);
    prompt.push_str("Member modules and files (cite these file:line anchors):\n");
    if members.is_empty() {
        prompt.push_str("- No members.\n");
    } else {
        append_evidence_table(
            &mut prompt,
            &["Name", "Kind", "Evidence", "Summary"],
            members,
        );
    }
    prompt.push_str("\nKey symbols (cite these file:line anchors):\n");
    if symbols.is_empty() {
        prompt.push_str("- No indexed symbols.\n");
    } else {
        append_evidence_table(
            &mut prompt,
            &["Symbol", "Kind", "Evidence", "Purpose"],
            symbols,
        );
    }
    append_source_excerpt_section_n(&mut prompt, sources, excerpt_take);
    prompt
}

fn append_evidence_table(prompt: &mut String, headers: &[&str; 4], rows: &[PageEvidenceRow]) {
    write_markdown_table_header(prompt, headers);
    for row in rows {
        write_markdown_table_row(
            prompt,
            [
                row.name.clone(),
                row.kind.clone(),
                row.citation.clone(),
                summary_excerpt(&row.summary),
            ],
        );
    }
}

fn build_entity_prompt(
    header: &str,
    entity_label: &str,
    entity: &str,
    files: &[ChildSummary],
    modules: &[ChildSummary],
    components: &[String],
    sources: &[SourceExcerpt],
) -> String {
    let mut prompt = format!("{header}\n\n{entity_label}: {entity}\n\n");
    append_table_guidance(&mut prompt);
    prompt.push_str("Files:\n");
    append_child_summary_sections(&mut prompt, files, modules, components);
    append_source_excerpt_section(&mut prompt, sources);
    prompt
}

fn append_child_summary_sections(
    prompt: &mut String,
    files: &[ChildSummary],
    modules: &[ChildSummary],
    components: &[String],
) {
    if files.is_empty() {
        prompt.push_str("- No direct files.\n");
    } else {
        append_child_summary_table(prompt, &["File", "Summary"], files);
    }
    prompt.push_str("\nChild modules:\n");
    if modules.is_empty() {
        prompt.push_str("- No child modules.\n");
    } else {
        append_child_summary_table(prompt, &["Module", "Summary"], modules);
    }
    prompt.push_str("\nStable component IDs:\n");
    if components.is_empty() {
        prompt.push_str("- No indexed components.\n");
    } else {
        append_component_table(prompt, components);
    }
}

fn append_source_excerpt_section(prompt: &mut String, sources: &[SourceExcerpt]) {
    append_source_excerpt_section_n(prompt, sources, MAX_PROMPT_SOURCE_EXCERPTS);
}

/// Like [`append_source_excerpt_section`] but with a caller-chosen excerpt
/// count, so per-page content passes can feed more grounded source than the
/// shared aggregate prompts without changing the aggregate budget.
fn append_source_excerpt_section_n(prompt: &mut String, sources: &[SourceExcerpt], take: usize) {
    prompt.push_str("\nSource excerpts:\n");
    if sources.is_empty() {
        prompt.push_str("- No source excerpts.\n");
        return;
    }
    for source in sources.iter().take(take) {
        let _ = writeln!(
            prompt,
            "--- {} (lines {}-{})",
            source.path, source.line_start, source.line_end
        );
        prompt.push_str(&bounded_excerpt(&source.excerpt));
        prompt.push('\n');
    }
}

/// Hard cap on one child summary embedded in an aggregate prompt. Aggregate
/// prompts roll up every child; unbounded summaries (citation walls, echoed
/// prompts recorded as summaries) once compounded up the module tree past
/// provider input limits (#698).
const CHILD_SUMMARY_EXCERPT_MAX_CHARS: usize = 2_000;

/// Hard cap on one retrieved source excerpt embedded in a prompt, and on how
/// many excerpts a single prompt may carry. Together with
/// [`CHILD_SUMMARY_EXCERPT_MAX_CHARS`] these keep aggregate prompts bounded
/// even though they now carry real source content.
pub(crate) const SOURCE_EXCERPT_MAX_CHARS: usize = 2_400;
pub(crate) const MAX_PROMPT_SOURCE_EXCERPTS: usize = 4;
/// Per-page content-pass excerpt budgets. Curated concept/narrative pages run a
/// dedicated second pass that is fed more source than the shared aggregate
/// prompts, so "rich input -> rich output" holds for the human-facing layer
/// without loosening the four pinned aggregate-prompt tests
/// ([`MAX_PROMPT_SOURCE_EXCERPTS`] stays at 4).
pub(crate) const CONCEPT_PAGE_SOURCE_EXCERPTS: usize = 8;
pub(crate) const NARRATIVE_PAGE_SOURCE_EXCERPTS: usize = 8;
/// Source-excerpt budget for the verification prompt: the auditor sees the same
/// breadth of cited evidence the page was generated against, so a claim grounded
/// in any fed excerpt is judged supported.
pub(crate) const VERIFY_SOURCE_EXCERPTS: usize = 8;
const ENUMERABLE_FACTS_GUIDANCE: &str = "When the supplied input exposes enumerable facts (CLI commands/flags, configuration keys, environment variables, or public API symbols), prefer compact Markdown tables beside the narrative instead of burying those facts in prose.";

/// First paragraph of a child summary, flattened to one line and hard-capped
/// at [`CHILD_SUMMARY_EXCERPT_MAX_CHARS`], so each prompt list entry stays one
/// bounded line regardless of what an earlier run recorded as the summary.
fn summary_excerpt(summary: &str) -> String {
    let paragraph = summary
        .trim()
        .split("\n\n")
        .next()
        .unwrap_or_default()
        .trim();
    let flattened = paragraph.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut excerpt = flattened;
    if excerpt.chars().count() > CHILD_SUMMARY_EXCERPT_MAX_CHARS {
        let body_cap = CHILD_SUMMARY_EXCERPT_MAX_CHARS.saturating_sub(1);
        let cap = excerpt
            .char_indices()
            .nth(body_cap)
            .map(|(index, _)| index)
            .unwrap_or(excerpt.len());
        excerpt.truncate(cap);
        excerpt.push('…');
    }
    excerpt
}

/// Source excerpt text hard-capped at [`SOURCE_EXCERPT_MAX_CHARS`]; newlines
/// are preserved because excerpts carry real source content.
fn bounded_excerpt(excerpt: &str) -> String {
    let trimmed = excerpt.trim_end();
    let cap = trimmed
        .char_indices()
        .nth(SOURCE_EXCERPT_MAX_CHARS)
        .map(|(index, _)| index);
    match cap {
        Some(cap) => {
            let mut bounded = trimmed[..cap].to_string();
            bounded.push('…');
            bounded
        }
        None => trimmed.to_string(),
    }
}

#[derive(Debug, Clone)]
pub struct SymbolSummary {
    pub name: String,
    pub kind: String,
    pub component_id: String,
    pub component_label: String,
    pub line_start: usize,
    pub line_end: usize,
    pub purpose: String,
}

#[derive(Debug, Clone)]
pub struct ChildSummary {
    pub name: String,
    pub summary: String,
}

/// A retrieved span of real source content fed into an aggregate prompt
/// alongside child summaries.
#[derive(Debug, Clone)]
pub struct SourceExcerpt {
    pub path: String,
    pub line_start: usize,
    pub line_end: usize,
    pub excerpt: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn oversized_child(name: &str) -> ChildSummary {
        let citation_wall = (0..2_000)
            .map(|line| format!("[src/lib.rs:{line}]"))
            .collect::<Vec<_>>()
            .join("\n");
        ChildSummary {
            name: name.to_string(),
            summary: format!("One real sentence.\n{citation_wall}"),
        }
    }

    #[test]
    fn aggregate_prompts_bound_each_child_summary() {
        let children = (0..3)
            .map(|index| oversized_child(&format!("src/module_{index}")))
            .collect::<Vec<_>>();

        for prompt in [
            module_prompt("src", &children, &children, &[], &[]),
            architecture_prompt("src", &children, &children, &[], &[]),
            repo_prompt(&children, &children, &[]),
        ] {
            for line in prompt.lines().filter(|line| line.starts_with("| src/")) {
                assert!(
                    line.chars().count()
                        <= CHILD_SUMMARY_EXCERPT_MAX_CHARS + "| src/module_0 |  |".chars().count(),
                    "child summary line stays bounded: {} chars",
                    line.chars().count()
                );
                assert!(line.contains('…'), "oversized excerpt is marked truncated");
            }
        }
    }

    #[test]
    fn short_summaries_pass_through_untruncated() {
        let child = ChildSummary {
            name: "src/lib.rs".to_string(),
            summary: "Concise healthy summary.".to_string(),
        };
        let prompt = module_prompt("src", &[child], &[], &[], &[]);
        assert!(prompt.contains("| src/lib.rs | Concise healthy summary. |\n"));
    }

    #[test]
    fn summary_excerpt_includes_ellipsis_inside_hard_cap() {
        let exact = "a".repeat(CHILD_SUMMARY_EXCERPT_MAX_CHARS);
        let oversized = format!("{exact}b");

        assert_eq!(
            summary_excerpt(&exact).chars().count(),
            CHILD_SUMMARY_EXCERPT_MAX_CHARS
        );
        let excerpt = summary_excerpt(&oversized);
        assert_eq!(excerpt.chars().count(), CHILD_SUMMARY_EXCERPT_MAX_CHARS);
        assert!(excerpt.ends_with('…'));
    }

    #[test]
    fn excerpt_flattens_multiline_summaries_to_one_line() {
        let child = ChildSummary {
            name: "src/lib.rs".to_string(),
            summary: "First line.\nSecond line of the same paragraph.".to_string(),
        };
        let prompt = module_prompt("src", &[child], &[], &[], &[]);
        assert!(
            prompt.contains("| src/lib.rs | First line. Second line of the same paragraph. |\n")
        );
    }

    #[test]
    fn aggregate_prompts_request_tables_for_enumerable_facts() {
        let child = ChildSummary {
            name: "src/cli.rs".to_string(),
            summary: "Defines commands and config keys.".to_string(),
        };
        let prompt = repo_prompt(&[child], &[], &[]);

        assert!(MODULE_SYSTEM.contains("compact Markdown tables"));
        assert!(REPO_SYSTEM.contains("CLI commands or flags"));
        assert!(ARCHITECTURE_SYSTEM.contains("public API symbols"));
        assert!(prompt.contains("Table guidance:\n"));
        assert!(
            prompt.contains("configuration keys, environment variables, or public API symbols")
        );
        assert!(prompt.contains("| Module | Summary |\n| --- | --- |\n"));
    }

    #[test]
    fn file_prompt_lists_symbols_as_markdown_table() {
        let symbol = SymbolSummary {
            name: "run|cli".to_string(),
            kind: "function".to_string(),
            component_id: "component|id".to_string(),
            component_label: "run [function]".to_string(),
            line_start: 7,
            line_end: 9,
            purpose: "Handles command dispatch.".to_string(),
        };

        let prompt = file_prompt("src/cli.rs", &[symbol], &[]);

        assert!(prompt.contains("| Symbol | Kind | Component | Component ID | Lines | Purpose |"));
        assert!(prompt.contains("| run\\|cli | function | run [function] | component\\|id | 7-9 | Handles command dispatch. |"));
    }

    fn excerpt(path: &str, content: &str) -> SourceExcerpt {
        SourceExcerpt {
            path: path.to_string(),
            line_start: 1,
            line_end: 40,
            excerpt: content.to_string(),
        }
    }

    #[test]
    fn aggregate_prompts_embed_bounded_source_excerpts() {
        let oversized = "x".repeat(SOURCE_EXCERPT_MAX_CHARS * 3);
        let sources = (0..MAX_PROMPT_SOURCE_EXCERPTS + 3)
            .map(|index| excerpt(&format!("src/file_{index}.rs"), &oversized))
            .collect::<Vec<_>>();

        let prompt = module_prompt("src", &[], &[], &[], &sources);

        let headers = prompt
            .lines()
            .filter(|line| line.starts_with("--- src/file_"))
            .count();
        assert_eq!(
            headers, MAX_PROMPT_SOURCE_EXCERPTS,
            "excerpt count is capped"
        );
        assert!(prompt.contains("--- src/file_0.rs (lines 1-40)"));
        for chunk in prompt.split("--- ").skip(1) {
            assert!(
                chunk.chars().count() <= SOURCE_EXCERPT_MAX_CHARS + 120,
                "each excerpt block stays bounded: {} chars",
                chunk.chars().count()
            );
        }
    }

    #[test]
    fn prompts_without_excerpts_state_their_absence() {
        let prompt = repo_prompt(&[], &[], &[]);
        assert!(prompt.contains("Source excerpts:\n- No source excerpts.\n"));
    }

    #[test]
    fn content_file_prompt_carries_leading_content() {
        let prompt = content_file_prompt(
            "README.md",
            &excerpt("README.md", "# Project\n\nWhat this repo does."),
        );
        assert!(prompt.contains("File: README.md"));
        assert!(prompt.contains("--- README.md (lines 1-40)"));
        assert!(prompt.contains("What this repo does."));
    }

    #[test]
    fn architecture_narrative_prompt_lists_subsystems_and_edges() {
        let subsystems = vec![
            ChildSummary {
                name: "crates/gcore".to_string(),
                summary: "Shared foundation library.".to_string(),
            },
            ChildSummary {
                name: "crates/gcode".to_string(),
                summary: "Code search CLI.".to_string(),
            },
        ];
        let edges = vec![("crates/gcode".to_string(), "crates/gcore".to_string())];

        let prompt = architecture_narrative_prompt(&subsystems, &edges);

        assert!(prompt.contains("- crates/gcore: Shared foundation library."));
        assert!(prompt.contains("- crates/gcode -> crates/gcore"));

        let empty = architecture_narrative_prompt(&subsystems, &[]);
        assert!(empty.contains("- No cross-subsystem dependency edges.\n"));
    }

    fn evidence(name: &str, kind: &str, citation: &str, summary: &str) -> PageEvidenceRow {
        PageEvidenceRow {
            name: name.to_string(),
            kind: kind.to_string(),
            citation: citation.to_string(),
            summary: summary.to_string(),
        }
    }

    #[test]
    fn concept_page_prompt_embeds_evidence_anchors_and_extra_excerpts() {
        let members = vec![evidence(
            "code/modules/src/search",
            "module",
            "src/search.rs:1-120",
            "Hybrid search entry point.",
        )];
        let symbols = vec![evidence(
            "query",
            "function",
            "src/search.rs:4-40",
            "Runs a hybrid search.",
        )];
        let sources = (0..6)
            .map(|index| excerpt(&format!("src/file_{index}.rs"), "fn demo() {}"))
            .collect::<Vec<_>>();

        let prompt = concept_page_prompt(
            "Search",
            "Hybrid search over the index.",
            &members,
            &symbols,
            &sources,
        );

        assert!(prompt.contains("src/search.rs:1-120"), "{prompt}");
        assert!(
            prompt.contains("| query | function | src/search.rs:4-40 |"),
            "{prompt}"
        );
        // Per-page budget is CONCEPT_PAGE_SOURCE_EXCERPTS (8), so a 5th excerpt
        // - which the shared aggregate cap of 4 would drop - is still present.
        assert!(prompt.contains("src/file_4.rs"), "{prompt}");
    }

    #[test]
    fn narrative_page_prompt_grounds_with_members_and_symbols() {
        let members = vec![evidence(
            "code/modules/src",
            "module",
            "src/lib.rs:1-50",
            "Crate root.",
        )];
        let symbols = vec![evidence(
            "Client",
            "struct",
            "src/lib.rs:1-1",
            "Public client.",
        )];

        let prompt = narrative_page_prompt("Introduction", "Start here.", &members, &symbols, &[]);

        assert!(prompt.contains("src/lib.rs:1-50"), "{prompt}");
        assert!(
            prompt.contains("| Client | struct | src/lib.rs:1-1 |"),
            "{prompt}"
        );
        assert!(prompt.contains("- No source excerpts.\n"), "{prompt}");
    }

    #[test]
    fn curated_page_systems_demand_grounded_multi_section_output() {
        for heading in [
            "## Purpose",
            "## Data flow",
            "## Key components",
            "## Where to start",
        ] {
            assert!(CONCEPT_PAGE_SYSTEM.contains(heading), "{heading}");
        }
        for heading in [
            "## Why this matters",
            "## How it works",
            "## What to read next",
        ] {
            assert!(NARRATIVE_PAGE_SYSTEM.contains(heading), "{heading}");
        }
        for system in [CONCEPT_PAGE_SYSTEM, NARRATIVE_PAGE_SYSTEM] {
            assert!(system.contains("file:line"), "{system}");
            assert!(system.contains("No markdown fences."), "{system}");
        }
    }
}
